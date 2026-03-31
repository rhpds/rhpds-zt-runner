
import sys
import os
import logging
import uuid
import json
import urllib.request
import ssl
import yaml
from ansible_runner import Runner, RunnerConfig
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path

import settings


def _load_user_data():
    '''
    Load user data and pass as extravars to every ansible-runner job.
    OCP mode: reads showroom-userdata ConfigMap via SA token.
    RHEL mode: reads from env vars (BASTION_HOST, ANSIBLE_USER, etc).
    This means playbooks can use {{ student_user }}, {{ guid }} etc
    directly without reading the ConfigMap themselves.
    '''
    extra = {}

    # --- OCP mode: SA token + showroom-userdata ConfigMap ---
    sa_token = Path('/var/run/secrets/kubernetes.io/serviceaccount/token')
    sa_ns = Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace')

    if sa_token.exists() and sa_ns.exists():
        try:
            token = sa_token.read_text().strip()
            namespace = sa_ns.read_text().strip()
            url = (f'https://kubernetes.default.svc/api/v1/namespaces/'
                   f'{namespace}/configmaps/showroom-userdata')
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                data = json.loads(resp.read())
                raw = data.get('data', {}).get('user_data.yml', '')
                if raw:
                    ud = yaml.safe_load(raw)
                    user = ud.get('user', '')
                    extra['student_user'] = user
                    extra['student_ns'] = f'{user}-zttest'
                    extra['student_ns2'] = f'{user}-ztworkspace'
                    extra['guid'] = ud.get('guid', os.getenv('GUID', ''))
                    extra['ocp_console_url'] = ud.get('openshift_console_url', '')
                    extra['ocp_api_url'] = ud.get('openshift_api_url', '')
                    # --- Bastion SSH vars (present for bastion+OCP labs) ---
                    if ud.get('bastion_public_hostname'):
                        extra['bastion_host'] = ud.get('bastion_public_hostname', '')
                        extra['bastion_port'] = str(ud.get('bastion_ssh_port', '22'))
                        extra['bastion_user'] = ud.get('bastion_ssh_user_name', 'lab-user')
                        extra['bastion_password'] = ud.get('bastion_ssh_password', '')
                        # For bastion+OCP labs student_user is the bastion SSH user
                        if not extra.get('student_user'):
                            extra['student_user'] = extra['bastion_user']
                        logger.info('Loaded bastion data from showroom-userdata CM: host=%s',
                                    extra['bastion_host'])
                    logger.info('Loaded user data from showroom-userdata CM: user=%s', user)

                    # --- Read zt-runner-kubeconfig Secret (written at provision time) ---
                    # No TokenRequest needed — pod SA reads its own namespace Secret.
                    # Playbooks use this kubeconfig, no showroom SA RBAC required.
                    kc_url = (f'https://kubernetes.default.svc/api/v1/namespaces/'
                              f'{namespace}/secrets/zt-runner-kubeconfig')
                    try:
                        kc_req = urllib.request.Request(
                            kc_url, headers={'Authorization': f'Bearer {token}'})
                        with urllib.request.urlopen(kc_req, context=ctx, timeout=5) as kr:
                            kc_data = json.loads(kr.read())
                            import base64, tempfile
                            kc_b64 = kc_data.get('data', {}).get('kubeconfig', '')
                            if kc_b64:
                                kc_content = base64.b64decode(kc_b64).decode()
                                kc_file = tempfile.NamedTemporaryFile(
                                    mode='w', suffix='.kubeconfig',
                                    delete=False, prefix='/tmp/zt-runner-')
                                kc_file.write(kc_content)
                                kc_file.flush()
                                extra['k8s_kubeconfig'] = kc_file.name
                                logger.info('Loaded kubeconfig from Secret (mode: %s)',
                                            kc_data.get('metadata', {}).get('labels', {}).get('mode', 'unknown'))
                    except Exception as kc_exc:
                        logger.debug('No zt-runner-kubeconfig Secret: %s', kc_exc)
        except Exception as exc:
            logger.warning('Could not load showroom-userdata ConfigMap: %s', exc)

    # --- RHEL / fallback: read from env vars ---
    if not extra.get('student_user'):
        bastion_user = os.getenv('ANSIBLE_USER', os.getenv('BASTION_USER', 'lab-user'))
        extra.setdefault('student_user', bastion_user)
        extra.setdefault('guid', os.getenv('GUID', ''))
        extra.setdefault('bastion_host', os.getenv('BASTION_HOST', ''))
        extra.setdefault('bastion_port', os.getenv('BASTION_PORT', '22'))
        extra.setdefault('bastion_user', bastion_user)
        extra.setdefault('bastion_password', os.getenv('ANSIBLE_PASSWORD', ''))
        logger.info('Loaded user data from env vars: bastion_user=%s', bastion_user)

    return extra

this = sys.modules[__name__]

this.executor = None

logger = logging.getLogger('uvicorn')

jobs = {}


class JobInfo:
    def __init__(self, ansible_job_id, status):
        self.ansible_job_id = ansible_job_id
        self.status = status
        self.lock = Lock()

    def set_status(self, status):
        with self.lock:
            self.status = status

    def get_status(self):
        status = ''
        with self.lock:
            status = self.status

        return status

    def get_ansible_job_id(self):
        return self.ansible_job_id


def init():
    '''
    Initialize jobs scheduler
    '''
    this.executor = ThreadPoolExecutor(max_workers=settings.max_workers)
    logger.info('Created thread pool with %d workers', settings.max_workers)

    logger.info('Jobs directory: %s', settings.jobs_path)


def shutdown():
    '''
    Shutdown jobs scheduler
    '''

    # Shutdown ThreadPoolExecutor after application is finished
    # all active ansible_runners will be finished gracefully while non-active
    # will be canceled
    this.executor.shutdown(cancel_futures=True)
    logger.info('Shutdown thread pool')


def worker_func(runner, job_id):
    '''
    Start Ansible Runner
    '''
    if job_id in jobs:
        jobs[job_id].set_status('running')

    status, rc = runner.run()

    if job_id in jobs:
        jobs[job_id].set_status(status)

    job_info_file = Path(
         f'{settings.base_dir}/'
         f'{settings.jobs_path}/'
         f'{str(job_id)}/ansible_job.json'
    )
    job_info_file.parent.mkdir(parents=True, exist_ok=True)
    job_info_file.write_text(
        json.dumps(
            {
                'ansible_job_id': jobs[job_id].get_ansible_job_id(),
                'status': status,
                'return_code': rc
            },
            indent=4
        )
    )

    logger.info('Job with ID: %s finished', job_id)


def create_job(module, stage):
    '''
    Create and schedule new ansible job
    '''
    job_id = uuid.uuid4()

    stage_file = Path(
        f'{settings.base_dir}/'
        f'{settings.scripts_path}/'
        f'{module}/'
        f'{stage}.yml'
    )
    if not stage_file.exists():
        logger.error('Stage file not found: %s', stage_file)
        return None

    extravars = {
        'module_dir': module,
        'module_stage': stage,
        'job_info_dir': (
            f'{settings.base_dir}/'
            f'{settings.jobs_path}/'
            f'{job_id}'
        ),
        **_load_user_data(),
    }

    rc = RunnerConfig(
        private_data_dir=f'{settings.base_dir}/{settings.scripts_path}',
        artifact_dir=f'{settings.base_dir}/{settings.artifacts_path}',
        extravars=extravars,
        playbook=f'{module}/{stage}.yml',
        quiet=True,
    )

    # TODO: Handle ConfigurationError exception
    rc.prepare()

    job_info = JobInfo(rc.ident, 'scheduled')
    job_info.set_status('scheduled')

    jobs[job_id] = job_info

    job_info_file = Path(
        f'{settings.base_dir}/'
        f'{settings.jobs_path}/'
        f'{job_id}/job_info.json'
    )
    job_info_file.parent.mkdir(parents=True, exist_ok=True)

    this.executor.submit(worker_func, Runner(config=rc), job_id)
    logger.info('Job with ID: %s scheduled', job_id)
    return job_id


def get_job_status(job_id):
    '''
    Get job status
    '''
    status = ''

    if job_id in jobs:
        status = jobs[job_id].get_status()

    return status


def get_job_output(job_id):
    '''
    Get job output
    '''
    job_output_file = Path(
        f'{settings.base_dir}/'
        f'{settings.jobs_path}/'
        f'{str(job_id)}/output.txt'
    )

    job_output = ''
    if job_output_file.exists():
        job_output = job_output_file.read_text()

    return job_output
