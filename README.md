# rhpds-zt-runner

RHDP Zero Touch (ZT) Ansible Runner API for lab grading.

Based on [ansible-runner-api](https://github.com/miteshget/ansible-runner-api)
by Mitesh Sharma, extended with `kubernetes.core` for OCP-native labs.

## Image

`quay.io/rhpds/zt-runner:latest`

## Custom Ansible Plugins

- `lab_check_fail` — writes error message to `job_info_dir/output.txt` and fails
- `validation_check` — conditional check, writes pass/fail message to `job_info_dir/output.txt`

## Usage in runtime-automation

Each module has standalone playbooks (no main.yml):
```
runtime-automation/
├── module-01/
│   ├── setup.yml       # Full playbook
│   ├── solve.yml       # Full playbook
│   └── validation.yml  # Full playbook using lab_check_fail / validation_check
└── module-02/
    └── ...
```

## Build

```bash
podman build -f Containerfile -t quay.io/rhpds/zt-runner:v2.0.0 --platform linux/amd64 .
podman push quay.io/rhpds/zt-runner:v2.0.0
```
