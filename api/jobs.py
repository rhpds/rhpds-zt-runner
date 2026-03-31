
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
                    logger.info('Loaded user data from showroom-userdata CM: user=%s', user)

                    # --- TokenRequest: get short-lived token for zt-runner SA ---
                    # Reads zt-runner-sa-config ConfigMap to find SA name/namespace,
                    # then calls TokenRequest API for a 1h token. No long-lived tokens stored.
                    cm_url = (f'https://kubernetes.default.svc/api/v1/namespaces/'
                              f'{namespace}/configmaps/zt-runner-sa-config')
                    try:
                        cm_req = urllib.request.Request(
                            cm_url, headers={'Authorization': f'Bearer {token}'})
                        with urllib.request.urlopen(cm_req, context=ctx, timeout=5) as cr:
                            cm_data = json.loads(cr.read()).get('data', {})
                            sa_name = cm_data.get('sa_name', 'zt-runner')
                            sa_ns = cm_data.get('sa_namespace', '')
                            api_url = cm_data.get('api_url', 'https://kubernetes.default.svc')

                            if sa_ns:
                                # Call TokenRequest API — 1h short-lived token
                                tr_url = (f'https://kubernetes.default.svc/api/v1/namespaces/'
                                          f'{sa_ns}/serviceaccounts/{sa_name}/token')
                                tr_body = json.dumps({
                                    'apiVersion': 'authentication.k8s.io/v1',
                                    'kind': 'TokenRequest',
                                    'spec': {'expirationSeconds': 3600}
                                }).encode()
                                tr_req = urllib.request.Request(
                                    tr_url, data=tr_body, method='POST',
                                    headers={
                                        'Authorization': f'Bearer {token}',
                                        'Content-Type': 'application/json'
                                    })
                                with urllib.request.urlopen(tr_req, context=ctx, timeout=5) as tr:
                                    tr_data = json.loads(tr.read())
                                    sa_token = tr_data['status']['token']

                                    # Build kubeconfig with short-lived token
                                    ca_path = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
                                    import base64
                                    ca_b64 = base64.b64encode(
                                        open(ca_path, 'rb').read()).decode()
                                    kubeconfig = (
                                        f'apiVersion: v1\nkind: Config\n'
                                        f'clusters:\n- name: cluster\n  cluster:\n'
                                        f'    server: "{api_url}"\n'
                                        f'    certificate-authority-data: "{ca_b64}"\n'
                                        f'contexts:\n- name: zt-runner\n  context:\n'
                                        f'    cluster: cluster\n    user: zt-runner\n'
                                        f'current-context: zt-runner\n'
                                        f'users:\n- name: zt-runner\n  user:\n'
                                        f'    token: "{sa_token}"\n'
                                    )
                                    import tempfile
                                    kc_file = tempfile.NamedTemporaryFile(
                                        mode='w', suffix='.kubeconfig',
                                        delete=False, prefix='/tmp/zt-runner-')
                                    kc_file.write(kubeconfig)
                                    kc_file.flush()
                                    extra['k8s_kubeconfig'] = kc_file.name
                                    logger.info('TokenRequest: got 1h token for %s/%s',
                                                sa_ns, sa_name)
                    except Exception as kc_exc:
                        logger.debug('No zt-runner-sa-config or TokenRequest failed: %s', kc_exc)
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
