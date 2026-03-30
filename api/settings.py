import os
from os.path import join, dirname
from dotenv import load_dotenv

dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)

# Number of workers
max_workers: int = int(os.getenv('MAX_WORKERS', 2))

# Web server details
host: str = os.getenv('HOST', '127.0.0.1')
port: int = int(os.getenv('PORT', 8000))
log_level: str = os.getenv('LOG_LEVEL', 'info')
reload: bool = bool(os.getenv('RELOAD', False))
root_path: str = os.getenv('ROOT_PATH', '/')

# Path to Ansible playbooks and Artifacts store)
base_dir: str = os.getenv('BASE_DIR', '/app')
scripts_path: str = os.getenv('SCRIPTS_PATH', 'runtime-automation')
artifacts_path: str = os.getenv('ARTIFACTS_PATH', 'artifacts')
jobs_path: str = os.getenv('JOBS_PATH', 'jobs')
