import os
import json
import logging
import urllib.request
import ssl
import yaml
from pathlib import Path


USER_DATA_FILE = '/user_data/user_data.yml'

logger = logging.getLogger('zt-runner.jobs')


def _load_user_data():
    '''
    Load user data and pass as extravars to every ansible-runner job.
    Priority:
      1. Mounted /user_data/user_data.yml (zerotouch chart mounts showroom-userdata CM here)
      2. OCP mode: reads showroom-userdata ConfigMap via SA token
      3. RHEL mode: reads from env vars (BASTION_HOST, ANSIBLE_USER, etc)
    '''
    extra = {}

    # --- Priority 1: mounted ConfigMap file (zerotouch chart) ---
    if Path(USER_DATA_FILE).exists():
        try:
            with open(USER_DATA_FILE) as f:
                ud = yaml.safe_load(f) or {}
            extra.update({k: v for k, v in ud.items() if isinstance(v, (str, int, float, bool))})
            user = ud.get('user', '')
            extra['student_user'] = user or ud.get('bastion_ssh_user_name', 'lab-user')
            extra['student_ns'] = f'{user}-zttest' if user else ''
            extra['student_ns2'] = f'{user}-ztworkspace' if user else ''
            extra['guid'] = ud.get('guid', os.getenv('GUID', ''))
            extra['bastion_host'] = ud.get('bastion_public_hostname', '')
            extra['bastion_port'] = str(ud.get('bastion_ssh_port', '22'))
            extra['bastion_user'] = ud.get('bastion_ssh_user_name', 'lab-user')
            extra['bastion_password'] = ud.get('bastion_ssh_password', '')
            logger.info('Loaded user data from mounted %s: user=%s', USER_DATA_FILE, extra.get('student_user'))
        except Exception as e:
            logger.warning('Failed to read %s: %s', USER_DATA_FILE, e)

    if extra:
        _try_load_kubeconfig_secret(extra)
        return extra

    # --- Priority 2: OCP mode via Kubernetes API ---
    sa_token = Path('/var/run/secrets/kubernetes.io/serviceaccount/token')
    sa_ns = Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace')

    if sa_token.exists() and sa_ns.exists():
        try:
            token = sa_token.read_text().strip()
            namespace = sa_ns.read_text().strip()
            url = (f'https://kubernetes.default.svc/api/v1/namespaces/'
                   f'{namespace}/configmaps/showroom-userdata')
            ctx = _k8s_ssl_context()
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
                    if ud.get('bastion_public_hostname'):
                        extra['bastion_host'] = ud.get('bastion_public_hostname', '')
                        extra['bastion_port'] = str(ud.get('bastion_ssh_port', '22'))
                        extra['bastion_user'] = ud.get('bastion_ssh_user_name', 'lab-user')
                        extra['bastion_password'] = ud.get('bastion_ssh_password', '')
                        if not extra.get('student_user'):
                            extra['student_user'] = extra['bastion_user']
                        logger.info('Loaded bastion data from showroom-userdata CM: host=%s',
                                    extra['bastion_host'])
                    logger.info('Loaded user data from showroom-userdata CM: user=%s', user)

                    _try_load_kubeconfig_secret(extra, token=token, namespace=namespace, ctx=ctx)
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


def _k8s_ssl_context():
    """Build an SSL context for in-cluster Kubernetes API calls."""
    ca_path = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    if os.path.exists(ca_path):
        return ssl.create_default_context(cafile=ca_path)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _try_load_kubeconfig_secret(extra, token=None, namespace=None, ctx=None):
    """Attempt to load kubeconfig from the zt-runner-kubeconfig Secret."""
    sa_token = Path('/var/run/secrets/kubernetes.io/serviceaccount/token')
    sa_ns = Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace')

    if token is None and sa_token.exists() and sa_ns.exists():
        token = sa_token.read_text().strip()
        namespace = sa_ns.read_text().strip()
    if token is None:
        return

    if ctx is None:
        ctx = _k8s_ssl_context()

    try:
        import base64
        import tempfile
        kc_url = (f'https://kubernetes.default.svc/api/v1/namespaces/'
                  f'{namespace}/secrets/zt-runner-kubeconfig')
        kc_req = urllib.request.Request(kc_url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(kc_req, context=ctx, timeout=5) as kr:
            kc_data = json.loads(kr.read())
            kc_b64 = kc_data.get('data', {}).get('kubeconfig', '')
            if kc_b64:
                kc_content = base64.b64decode(kc_b64)
                kc_file = tempfile.NamedTemporaryFile(
                    mode='wb', suffix='.kubeconfig',
                    delete=False, prefix='zt-runner-')
                kc_file.write(kc_content)
                kc_file.close()
                extra['k8s_kubeconfig'] = kc_file.name
                logger.info('Loaded kubeconfig from Secret')
    except Exception as e:
        logger.debug('No zt-runner-kubeconfig Secret: %s', e)
