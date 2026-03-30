
import sys
import logging
import uuid
import json
from ansible_runner import Runner, RunnerConfig
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path

import settings

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
        )
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
