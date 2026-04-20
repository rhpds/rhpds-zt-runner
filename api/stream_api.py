#!/usr/bin/env python3
"""
Flask SSE API server for classic Showroom solve/validate.

Runs on port 80. Nginx proxies /stream/* → this server (strips /stream prefix).

Endpoints (called as /stream/<endpoint> from browser):
  GET /health                  — health check
  GET /config                  — list available modules
  GET /solve/<module>          — stream solve output (SSE)
  GET /validate/<module>       — stream validation output (validate.yml or validation.yml)
  GET /setup/<module>          — stream setup output

RUNTIME_DIR: BASE_DIR env var + /runtime-automation (default: /app/runtime-automation)

Extravar injection (from showroom-userdata CM):
  - k8s_kubeconfig (OCP labs)
  - student_ns, student_ns2, student_user, guid
  - bastion_host, bastion_port, bastion_user, bastion_password (bastion labs)
"""

import os
import re
import json
import time
import queue
import threading
import tempfile
import subprocess
import logging

from flask import Flask, Response, jsonify, abort
from flask_cors import CORS

MAX_CONCURRENT_PLAYBOOKS = int(os.environ.get('MAX_CONCURRENT_PLAYBOOKS', 4))
_playbook_semaphore = threading.Semaphore(MAX_CONCURRENT_PLAYBOOKS)

# Reuse existing extravar injection from jobs.py
from jobs import _load_user_data

LOG_DIR = '/tmp/playbook-logs'

# Auto-detect runtime-automation path:
# - /showroom/repo/runtime-automation  (OCP zerotouch chart — git-cloner clones here)
# - /app/runtime-automation            (RHEL vm_workload_showroom — mounted from host)
# - override with RUNTIME_AUTOMATION_DIR env var
# Zerotouch chart mounts runtime-automation at /app/runtime-automation
# when runtime_automation.setup=true. Otherwise full repo at /showroom/repo.
# RHEL (vm_workload_showroom) mounts at /app/runtime-automation.
# Check explicit mount first, fallback to full repo, override with BASE_DIR.
_base = (
    '/app' if os.path.isdir('/app/runtime-automation')
    else '/showroom/repo'
)
RUNTIME_DIR = os.path.join(os.environ.get('BASE_DIR', _base), 'runtime-automation')

os.makedirs(LOG_DIR, exist_ok=True)

stream_app = Flask(__name__)
CORS(stream_app)
logger = logging.getLogger('stream_api')

_MODULE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$')


def _validated_module_dir(module_name):
    """Validate module_name to prevent path traversal and return its absolute path."""
    if not _MODULE_RE.match(module_name):
        return None
    candidate = os.path.realpath(os.path.join(RUNTIME_DIR, module_name))
    if not candidate.startswith(os.path.realpath(RUNTIME_DIR) + os.sep):
        return None
    return candidate


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


_lab_reqs_installed = False


def _ensure_lab_requirements():
    """Install lab requirements once on first request, not at import time."""
    global _lab_reqs_installed
    if _lab_reqs_installed:
        return
    _lab_reqs_installed = True
    _install_lab_requirements()


@stream_app.before_request
def _before_request():
    _ensure_lab_requirements()


def _build_extravars_file():
    """Write all injected extravars to a temp file for ansible-playbook -e @file.
    jobs.py _load_user_data() checks mounted /user_data/user_data.yml first,
    then falls back to Kubernetes CM API, then to env vars (RHEL).
    """
    extravars = _load_user_data()
    kubeconfig = extravars.pop('k8s_kubeconfig', '')
    fd = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False, prefix='zt-extravars-')
    try:
        os.fchmod(fd.fileno(), 0o600)
        for k, v in extravars.items():
            fd.write(f"{k}: {json.dumps(v)}\n")
    finally:
        fd.close()
    return fd.name, kubeconfig


def _run_playbook(playbook_path, output_queue):
    """Execute ansible-playbook and stream output via queue."""
    if not os.path.exists(playbook_path):
        output_queue.put(f"ERROR: Playbook not found: {playbook_path}\n")
        output_queue.put('__DONE__')
        return

    if not _playbook_semaphore.acquire(timeout=5):
        output_queue.put(f"ERROR: Too many concurrent playbooks (max {MAX_CONCURRENT_PLAYBOOKS}). Try again.\n")
        output_queue.put('__DONE__')
        return

    log_file = os.path.join(LOG_DIR, f"{os.path.basename(playbook_path)}-{int(time.time())}.log")
    job_info_dir = tempfile.mkdtemp(prefix='zt-job-info-')
    vars_file = None
    kubeconfig = None

    try:
        vars_file, kubeconfig = _build_extravars_file()

        cmd = ['ansible-playbook', playbook_path]

        verbosity = os.environ.get('ANSIBLE_VERBOSITY', '').strip()
        if verbosity:
            cmd.append(verbosity)

        cmd.extend(['-e', f'@{vars_file}', '-e', f'job_info_dir={job_info_dir}'])
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

        while True:
            line = tail.stdout.readline()
            if line:
                output_queue.put(line)
            elif proc.poll() is not None:
                break
        try:
            tail.terminate()
            tail.wait(timeout=2)
        except Exception:
            pass

        proc.wait()

        if proc.returncode == 0:
            output_queue.put('\n✓ Completed successfully!\n')
        else:
            output_queue.put(f'\n✗ Failed (exit code {proc.returncode})\n')

    except Exception as e:
        output_queue.put(f'\nERROR: {e}\n')
    finally:
        _playbook_semaphore.release()
        output_queue.put('__DONE__')
        for path in (vars_file, kubeconfig):
            try:
                if path and os.path.isfile(path):
                    os.unlink(path)
            except OSError:
                pass
        try:
            import shutil
            shutil.rmtree(job_info_dir, ignore_errors=True)
        except Exception:
            pass
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
                line = q.get(timeout=15)
                if line == '__DONE__':
                    yield "data: __DONE__\n\n"
                    break
                yield f"data: {json.dumps(line)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@stream_app.route('/health')
def health():
    checks = {'api': 'ok'}
    status_code = 200

    if not os.path.isdir(RUNTIME_DIR):
        checks['runtime_dir'] = f'missing: {RUNTIME_DIR}'
        status_code = 503
    else:
        checks['runtime_dir'] = 'ok'

    try:
        subprocess.run(
            ['ansible-playbook', '--version'],
            capture_output=True, timeout=5, check=True
        )
        checks['ansible'] = 'ok'
    except Exception:
        checks['ansible'] = 'unavailable'
        status_code = 503

    checks['status'] = 'healthy' if status_code == 200 else 'degraded'
    return jsonify(checks), status_code


@stream_app.route('/config')
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


@stream_app.route('/solve/<module_name>')
def solve(module_name):
    module_dir = _validated_module_dir(module_name)
    if module_dir is None:
        abort(400, description=f"Invalid module name: {module_name}")
    playbook = os.path.join(module_dir, 'solve.yml')
    return _sse_stream(playbook, f'solve for {module_name}')


@stream_app.route('/validate/<module_name>')
def validate(module_name):
    module_dir = _validated_module_dir(module_name)
    if module_dir is None:
        abort(400, description=f"Invalid module name: {module_name}")
    for name in ('validate.yml', 'validation.yml'):
        p = os.path.join(module_dir, name)
        if os.path.exists(p):
            return _sse_stream(p, f'validation for {module_name}')
    return _sse_stream(os.path.join(module_dir, 'validate.yml'),
                       f'validation for {module_name}')


@stream_app.route('/setup/<module_name>')
def setup(module_name):
    module_dir = _validated_module_dir(module_name)
    if module_dir is None:
        abort(400, description=f"Invalid module name: {module_name}")
    playbook = os.path.join(module_dir, 'setup.yml')
    return _sse_stream(playbook, f'setup for {module_name}')
