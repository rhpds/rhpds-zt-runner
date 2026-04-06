#!/usr/bin/env python3
"""
Flask SSE API server for classic Showroom solve/validate (Option B - Tyrell's approach).

Endpoints:
  GET /stream/config               — list available modules
  GET /stream/solve/<module>       — run solve playbook, stream output via SSE
  GET /stream/validate/<module>    — run validation playbook, stream output via SSE
  GET /stream/health               — health check

Extravar injection (from showroom-userdata CM):
  - k8s_kubeconfig (OCP labs)
  - student_ns, student_ns2, student_user, guid
  - bastion_host, bastion_port, bastion_user, bastion_password (bastion labs)
"""

import os
import json
import time
import queue
import threading
import tempfile
import subprocess
import logging

from flask import Flask, Response, jsonify

# Reuse existing extravar injection from jobs.py
from jobs import _load_user_data, _write_kubeconfig

LOG_DIR = '/tmp/playbook-logs'
RUNTIME_DIR = os.environ.get('BASE_DIR', '/app') + '/runtime-automation'

os.makedirs(LOG_DIR, exist_ok=True)

stream_app = Flask(__name__)
logger = logging.getLogger('stream_api')


def _install_lab_requirements():
    """
    Install lab-specific Python packages and Ansible collections from
    requirements files in the runtime-automation directory.

    Supported files:
      runtime-automation/requirements.txt  → pip install
      runtime-automation/requirements.yml  → ansible-galaxy collection install
    """
    pip_reqs = os.path.join(RUNTIME_DIR, 'requirements.txt')
    galaxy_reqs = os.path.join(RUNTIME_DIR, 'requirements.yml')

    if os.path.exists(pip_reqs):
        logger.info('Installing lab Python packages from requirements.txt')
        try:
            subprocess.run(
                ['pip', 'install', '--quiet', '-r', pip_reqs],
                check=True, capture_output=True
            )
            logger.info('Lab Python packages installed')
        except subprocess.CalledProcessError as e:
            logger.warning('Failed to install lab packages: %s', e.stderr.decode())

    if os.path.exists(galaxy_reqs):
        logger.info('Installing lab Ansible collections from requirements.yml')
        try:
            subprocess.run(
                ['ansible-galaxy', 'collection', 'install', '-r', galaxy_reqs],
                check=True, capture_output=True
            )
            logger.info('Lab Ansible collections installed')
        except subprocess.CalledProcessError as e:
            logger.warning('Failed to install lab collections: %s', e.stderr.decode())


# Install lab requirements at startup
_install_lab_requirements()


def _build_extravars_file():
    """Write all injected extravars to a temp file for ansible-playbook -e @file."""
    extravars = _load_user_data()
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False)
    for k, v in extravars.items():
        # Write as YAML key: value (skip kubeconfig path — passed separately)
        if k != 'k8s_kubeconfig':
            tmp.write(f"{k}: {json.dumps(v)}\n")
    tmp.close()
    return tmp.name, extravars.get('k8s_kubeconfig', '')


def _run_playbook(playbook_path, output_queue):
    """Execute ansible-playbook and stream output via queue."""
    if not os.path.exists(playbook_path):
        output_queue.put(f"ERROR: Playbook not found: {playbook_path}\n")
        output_queue.put('__DONE__')
        return

    log_file = os.path.join(LOG_DIR, f"{os.path.basename(playbook_path)}-{int(time.time())}.log")

    try:
        vars_file, kubeconfig = _build_extravars_file()

        cmd = ['ansible-playbook', playbook_path, '-v', '-e', f'@{vars_file}']
        if kubeconfig:
            cmd += ['-e', f'k8s_kubeconfig={kubeconfig}']

        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['ANSIBLE_FORCE_COLOR'] = '0'

        open(log_file, 'w').close()

        tail = subprocess.Popen(
            ['tail', '-f', log_file],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True
        )

        with open(log_file, 'w') as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)

        done = False
        while not done:
            line = tail.stdout.readline()
            if line:
                output_queue.put(line)
                if 'PLAY RECAP' in line and '*' in line:
                    done = True
                    tail.terminate()
            elif proc.poll() is not None:
                break

        time.sleep(0.2)
        with open(log_file) as f:
            for line in f.readlines()[-5:]:
                output_queue.put(line)

        try:
            tail.wait(timeout=1)
        except Exception:
            pass

        proc.wait()
        os.unlink(vars_file)

        if proc.returncode == 0:
            output_queue.put('\n✓ Completed successfully!\n')
        else:
            output_queue.put(f'\n✗ Failed (exit code {proc.returncode})\n')

    except Exception as e:
        output_queue.put(f'\nERROR: {e}\n')
    finally:
        output_queue.put('__DONE__')
        # Keep last 10 logs
        try:
            logs = sorted([f for f in os.listdir(LOG_DIR) if f.endswith('.log')])
            for old in logs[:-10]:
                os.remove(os.path.join(LOG_DIR, old))
        except Exception:
            pass


def _sse_stream(playbook_path, label):
    """Generator that runs a playbook and yields SSE events."""
    def generate():
        q = queue.Queue()
        t = threading.Thread(target=_run_playbook, args=(playbook_path, q), daemon=True)
        t.start()

        yield f"data: Starting {label}...\n\n"

        while True:
            try:
                line = q.get(timeout=0.1)
                if line == '__DONE__':
                    yield "data: __DONE__\n\n"
                    break
                yield f"data: {json.dumps(line)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype='text/event-stream')


@stream_app.route('/stream/health')
def health():
    return jsonify({'status': 'healthy'}), 200


@stream_app.route('/stream/config')
def config():
    """List available modules by scanning runtime-automation/module-*/."""
    modules = {}
    if os.path.isdir(RUNTIME_DIR):
        for entry in sorted(os.listdir(RUNTIME_DIR)):
            path = os.path.join(RUNTIME_DIR, entry)
            if os.path.isdir(path) and entry.startswith('module-'):
                stages = [
                    s.replace('.yml', '')
                    for s in os.listdir(path)
                    if s.endswith('.yml')
                ]
                modules[entry] = sorted(stages)
    return jsonify(modules), 200


@stream_app.route('/stream/solve/<module_name>')
def solve(module_name):
    playbook = os.path.join(RUNTIME_DIR, module_name, 'solve.yml')
    return _sse_stream(playbook, f'solve for {module_name}')


@stream_app.route('/stream/validate/<module_name>')
def validate(module_name):
    playbook = os.path.join(RUNTIME_DIR, module_name, 'validation.yml')
    return _sse_stream(playbook, f'validation for {module_name}')


@stream_app.route('/stream/setup/<module_name>')
def setup(module_name):
    playbook = os.path.join(RUNTIME_DIR, module_name, 'setup.yml')
    return _sse_stream(playbook, f'setup for {module_name}')
