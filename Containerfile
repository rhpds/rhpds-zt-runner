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

ARG OC_VERSION=4.20.18

WORKDIR /app/

USER root

RUN dnf install -y sshpass nodejs npm \
    nss nspr atk at-spi2-atk cups-libs libXcomposite libXdamage \
    libXfixes libXrandr libgbm libxkbcommon pango alsa-lib && \
    dnf clean all

# ── Playwright (headless browser for UI-based lab steps) ──────────────────────
# Installs playwright + Chromium so solve.yml can call Playwright .js scripts
# via ansible.builtin.script for steps that require browser automation.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
ENV NODE_PATH=/usr/local/lib/node_modules
RUN npm install -g playwright && \
    npx playwright install chromium && \
    chmod -R g+rwX /app/.playwright

RUN curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/openshift-client-linux.tar.gz | \
  tar xz -C /usr/local/bin oc kubectl && \
  chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

RUN chown -R ${USER_UID}:0 /app

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

# ── Runner API (Flask + Gunicorn SSE server) ──────────────────────────────────
COPY api/ /app/

# ── Fix permissions for OpenShift random UID (gid=0 must write) ──────────────
# ansible-galaxy creates .ansible/tmp as root — must be group-writable
RUN mkdir -p /opt/app-root/src/.ansible/tmp && \
    chmod -R g+rwX /opt/app-root/src/.ansible && \
    chown -R 1001:0 /opt/app-root/src/.ansible && \
    chmod -R g+rwX /usr/share/ansible && \
    chmod -R g+rwX /app

ENV HOST="0.0.0.0"
ENV PORT=80

USER ${USER_UID}

CMD ["python", "main.py"]
