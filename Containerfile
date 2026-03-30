# =============================================================================
# RHDP ZT Runner — Ansible Runner API for Zero Touch labs
#
# Based on the ansible-runner-api pattern by Mitesh Sharma (mitsharm@redhat.com)
# Extended with kubernetes.core for OCP-native labs (no SSH/bastion needed).
#
# Includes custom Ansible action plugins:
#   - lab_check_fail     — write error to job_info_dir and fail
#   - validation_check   — conditional check with pass/fail message to file
#
# Source: https://github.com/rhpds/rhpds-zt-runner
# Image:  quay.io/rhpds/zt-runner
# =============================================================================
FROM registry.access.redhat.com/ubi9/python-311

WORKDIR /app/

USER root

RUN dnf install -y sshpass && dnf clean all && \
    chown -R ${USER_UID}:0 /app

# ── Python packages ───────────────────────────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    pip install --no-cache-dir \
        kubernetes \
        jmespath \
        netaddr

# ── Ansible Collections ───────────────────────────────────────────────────────
RUN ansible-galaxy collection install \
    kubernetes.core \
    ansible.posix \
    community.general \
    community.crypto \
    community.hashi_vault \
    --collections-path /usr/share/ansible/collections

# ── Custom Ansible action plugins (Mitesh's lab_check_fail + validation_check) ─
RUN mkdir -p /usr/share/ansible/plugins/action
COPY ansible-plugins/action/ /usr/share/ansible/plugins/action/

# ── Runner API (FastAPI + ansible-runner) ─────────────────────────────────────
COPY api/ /app/

# ── Fix permissions for OpenShift random UID (gid=0 must write) ──────────────
# ansible-galaxy creates .ansible/tmp as root — must be group-writable
RUN mkdir -p /opt/app-root/src/.ansible/tmp && \
    chmod -R g+rwX /opt/app-root/src/.ansible && \
    chown -R 1001:0 /opt/app-root/src/.ansible && \
    chmod -R g+rwX /usr/share/ansible && \
    chmod -R g+rwX /app

ENV BASE_DIR="/app"
ENV HOST="0.0.0.0"
ENV PORT=8501

USER ${USER_UID}

CMD ["python", "main.py"]
