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
FROM registry.access.redhat.com/ubi9/python-311:9.7

ARG OC_VERSION=4.20.18

USER root

# ── System packages ──────────────────────────────────────────────────────────
RUN dnf install -y sshpass nodejs npm \
    nss nspr atk at-spi2-atk cups-libs libXcomposite libXdamage \
    libXfixes libXrandr libgbm libxkbcommon pango alsa-lib && \
    dnf clean all

# ── Playwright (headless Chromium for browser-based lab steps) ───────────────
RUN mkdir -p /app
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
ENV NODE_PATH=/usr/local/lib/node_modules
RUN npm install -g playwright && \
    npx playwright install chromium && \
    chmod -R g+rwX /app/.playwright

# ── OpenShift CLI ────────────────────────────────────────────────────────────
RUN curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${OC_VERSION}/openshift-client-linux.tar.gz | \
    tar xz -C /usr/local/bin oc kubectl && \
    chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

# ── Python packages (via S2I assemble) ──────────────────────────────────────
USER 0
COPY requirements.txt /tmp/src/requirements.txt
RUN /usr/bin/fix-permissions /tmp/src
USER 1001
RUN /usr/libexec/s2i/assemble

# ── Ansible Collections ─────────────────────────────────────────────────────
USER root
RUN ansible-galaxy collection install \
    kubernetes.core \
    ansible.posix \
    community.general \
    community.crypto \
    community.hashi_vault \
    --collections-path /usr/share/ansible/collections

# ── Custom Ansible action plugins ───────────────────────────────────────────
RUN mkdir -p /usr/share/ansible/plugins/action
COPY ansible-plugins/action/ /usr/share/ansible/plugins/action/

# ── Runner API (Flask + Gunicorn SSE server) ────────────────────────────────
COPY api/ /app/

# ── Permissions for OpenShift random UID (gid=0 must write) ─────────────────
RUN mkdir -p /opt/app-root/src/.ansible/tmp && \
    chmod -R g+rwX /opt/app-root/src/.ansible && \
    chown -R 1001:0 /opt/app-root/src/.ansible && \
    chmod -R g+rwX /usr/share/ansible && \
    chmod -R g+rwX /app

ENV HOST="0.0.0.0"
ENV PORT=80

WORKDIR /app
USER 1001

CMD ["python", "main.py"]
