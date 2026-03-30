#!/usr/bin/python

# Copyright: (c) 2024, Mitesh Sharma <mitsharm@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import annotations

import os
from ansible.errors import AnsibleError, AnsibleUndefinedVariable
from ansible.plugins.action import ActionBase
from ansible.playbook.conditional import Conditional
from ansible.module_utils.six import string_types
from ansible.module_utils.parsing.convert_bool import boolean

DOCUMENTATION = """
        action: validation_check
        author: Mitesh Sharma <mitsharm@redhat.com>
        version_added: "2.9"
        short_description: Validation check with custom message and write result to file
        description:
            - This module performs a validation check based on provided conditions.
            - If the condition fails, a custom error message is written to a file and the task fails.
            - If the condition passes, a custom success message is written to a file and the task passes.
        Variables:
            job_info_dir:
                description:
                  - job_info_dir global variable should be defined as directory path
                  - where error or pass log output will be written.
        options:
            error_msg:
                description:
                    - Custom message to be written to the file if the task fails.
                    - Required if pass_msg is not used.
                required: False
                type: string
            pass_msg:
                description:
                    - Custom message to be written to the file if the task passes.
                    - Required if error_msg is not used.
                required: False
                type: string
            check:
                description: Condition(s) to test.
                required: True
                type: list
            
"""

EXAMPLES = """
- name: Get stats of the inventory file
  ansible.builtin.stat:
    path: /home/rhel/ansible-files/inventory
    register: r_hosts

# This message will be written to a file and the task will fail if the inventory file does not exist
- name: Write error message and fail the task if inventory file is missing
  validation_check:
    error_msg: "Inventory file does not exist"
    check: r_hosts.stat.exists #(False)

# This message will be written to a file and the task will pass if the inventory file exists
- name: Write success message if inventory file exists
  validation_check:
    pass_msg: "Inventory file exists"
    check: r_hosts.stat.exists #(True)

# If both error_msg and pass_msg are provided, the appropriate message will be used based on the condition
- name: Write appropriate message based on inventory file presence
  validation_check:
    error_msg: "Inventory file does not exist (False)"
    pass_msg: "Inventory file exists (True)"
    check: r_hosts.stat.exists #(True/False)
"""

class ActionModule(ActionBase):
    
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset(('error_msg', 'pass_msg', 'check'))
    _requires_connection = False

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = {}

        result = super(ActionModule, self).run(tmp, task_vars)
        del tmp
        
        # Validate 'check' argument presence
        if 'check' not in self._task.args:
            raise AnsibleError('The "check" parameter is required.')
        
        error_msg = None
        pass_msg = None
        e_message = self._task.args.get('error_msg')
        p_message = self._task.args.get('pass_msg')
        
        # Ensure at least one of error_msg or pass_msg is provided
        if e_message is None and p_message is None:
            raise AnsibleError('At least one of error_msg or pass_msg must be provided.')
        
        # Validate error_msg type
        if e_message != None:
            e_message = e_message
            if isinstance(e_message, list):
                if not all(isinstance(x, string_types) for x in e_message):
                    raise AnsibleError('All elements in error_msg list must be strings.')
            elif not isinstance(e_message, (string_types, list)):
                raise AnsibleError('error_msg must be a string or a list of strings.')
            
        # Validate pass_msg type
        if p_message != None:
            p_message = p_message
            if isinstance(p_message, list):
                if not all(isinstance(x, string_types) for x in p_message):
                    raise AnsibleError('All elements in pass_msg list must be strings.')
            elif not isinstance(p_message, (string_types, list)):
                raise AnsibleError('pass_msg must be a string or a list of strings.')
        
         # Ensure 'check' is a list
        conditions = self._task.args['check']
        if not isinstance(conditions, list):
            conditions = [conditions]

        # Output directory Path
        output_dir = task_vars.get('job_info_dir', None)
        if output_dir is None:
            raise AnsibleError('The job_info_dir variable must be defined.')
        
        # Output file path
        output_result_path = os.path.join(output_dir, 'output.txt')
        
        # Initialize Conditional object
        cond = Conditional(loader=self._loader)
        result['_ansible_verbose_always'] = True
        
        for condition in conditions:
            cond.when = [condition]
            test_result = cond.evaluate_conditional(templar=self._templar, all_vars=task_vars)
            try:
                
                if not test_result and e_message != None:
                    f = open(output_result_path, 'w')
                    f.write(e_message)
                    result['failed'] = True
                    result['evaluated_to'] = test_result
                    result['condition'] = condition
                    result['msg'] = f"{e_message} : Message written to log"
                    return result
                elif test_result and p_message != None:
                    f = open(output_result_path, 'w')
                    f.write(p_message)
                    result['changed'] = True
                    result['evaluated_to'] = test_result
                    result['condition'] = condition
                    result['msg'] = f"{p_message} : Message written to log"
                    return result
                else:
                    result['skipped'] = True
                    result['condition'] = condition
                    return result

            except Exception as e:
                    result['failed'] = True
                    result['msg'] = f"Failed to write message to log: {e}"
                    return result
