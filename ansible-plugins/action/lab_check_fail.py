#!/usr/bin/python

# Copyright: (c) 2024, Mitesh Sharma <mitsharm@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import annotations

import os
from ansible.errors import AnsibleError, AnsibleUndefinedVariable
from ansible.plugins.action import ActionBase

DOCUMENTATION = """
        action: lab_check_fail
        author: Mitesh Sharma <mitsharm@redhat.com>
        version_added: "2.9"
        short_description: Validation check with custom message and write result to file
        description:
           - This module write a custom error message is written to a file and the task fails.
        Variables:
          job_info_dir:
            description:
              - job_info_dir global variable should be defined as directory path
              - where error or pass log output will be written.
        options:
          msg:
            description: Custom message to be written to the file.
            required: True
            type: string
            default: False
"""

EXAMPLES = """
- name: Get stats of the inventory file
  ansible.builtin.stat:
    path: /home/rhel/ansible-files/inventory
    register: r_hosts
    
# This message will be written to a file and the task will fail if the inventory file does not exist
- name: Write error message and fail the task if inventory file is missing
  when: not r_hosts.stat.exists
  lab_check_fail:
    msg: "Inventory file does not exist"
"""


class ActionModule(ActionBase):

    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset(('msg',))
    _requires_connection = False

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp  # tmp no longer has any effect
            
        # Validate 'msg' argument presence
        if 'msg' not in self._task.args:
            raise AnsibleError('The "msg" parameter is required.')
        else:
            msg = self._task.args.get('msg')
        
        # Output Directory Path
        output_dir = task_vars.get('job_info_dir', None)
        if output_dir is None:
            raise AnsibleError("The job_info_dir variable must be defined")
        
        # Output file path
        output_result_path = os.path.join(output_dir, 'output.txt')
        
        result['_ansible_verbose_always'] = True
        
        try:
            f = open(output_result_path, 'w')
            f.write(msg)
            result['failed'] = True
            result['msg'] = f"{msg} - Message written to log"
            return result
                
        except Exception as e:
                result['failed'] = True
                result['msg'] = f"Failed to write message to log: {e}"
                return result
