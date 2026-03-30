'''
Validation Scripts API
'''
import logging
from uuid import UUID
from http import HTTPStatus
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException

import settings
import jobs

logger = logging.getLogger('uvicorn')


@asynccontextmanager
async def lifespan(application: FastAPI):
    '''
    Initialization and shutdown events
    '''
    logger.info(
        'Artifacts directory: %s',
        f'{settings.base_dir}/{settings.artifacts_path}'
    )

    logger.info(
        'Ansible Runner directory: %s',
        f'{settings.base_dir}/{settings.scripts_path}'
    )

    logger.info(
        'Root path: %s',
        settings.root_path
    )

    if settings.reload:
        logger.warning(
            'Reloader should\'t not be used'
            'in production environment'
        )

    jobs.init()

    yield

    jobs.shutdown()

app = FastAPI(
    lifespan=lifespan,
    root_path=settings.root_path
)


@app.post("/api/{module}/{stage}", status_code=HTTPStatus.ACCEPTED)
async def run_task(module: str, stage: str):
    '''
    Create and schedule new job
    '''
    job_id = jobs.create_job(module, stage)

    if job_id is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f'Job {module}/{stage} not found'
        )

    return {'Job_id': job_id}


@app.get("/api/job/{uid}")
async def get_job(uid: UUID):
    '''
    Get job status
    '''
    status = jobs.get_job_status(uid)
    if not status:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f'Job {str(uid)} not found'
        )
    output = jobs.get_job_output(uid)

    return {'Status': status, 'Output': output}


if __name__ == '__main__':
    uvicorn.run(
        'main:app',
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=settings.reload,
    )
