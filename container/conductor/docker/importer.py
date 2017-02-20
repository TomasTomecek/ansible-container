# -*- coding: utf-8 -*-
from __future__ import absolute_import
import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import itertools
import subprocess
import shlex
import urlparse
import tarfile
import glob

try:
    import ruamel.yaml
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.error import Mark
    from ruamel.yaml.tokens import CommentToken
except ImportError:
    raise ImportError('This engine requires you "pip install \'ruamel.yaml>=0.13.13\'" to import projects.')

from ..utils import create_role_from_templates
from ..exceptions import AnsibleContainerConductorException

# Known issues:
# * In a Dockerfile, ENV and ARG params don't apply to directives before their
#   declaration. In our importer, they do.
# * We try to split multi-command RUN into multiple tasks. This only works if
#   the Dockerfile is reasonably well formed.
class CommentableDict(dict):
    comments = []
    def __init__(self, obj, comments):
        self.comments = comments
        super(CommentableDict, self).__init__(obj)

class CommentableList(list):
    comments = []
    def __init__(self, obj, comments):
        self.comments = comments
        super(CommentableList, self).__init__(obj)

class CommentableString(basestring):
    comments = []
    def __init__(self, obj, comments):
        self.comments = comments
        super(CommentableString, self).__init__(obj)


class DockerfileParser(object):
    escape_char = u'\\'
    path = None
    docker_file_path = None
    environment = {}
    variables = {}
    meta = {}
    shell = None
    user = None
    working_dir = None

    # These directives support JSON arrays as arguments
    supports_json = {
        'RUN',
        'CMD',
        'ADD',
        'COPY',
        'ENTRYPOINT',
        'VOLUME',
        'SHELL',
    }

    # These directives support environment variable substitution
    supports_env = {
        'ADD',
        'COPY',
        'ENV',
        'EXPOSE',
        'LABEL',
        'USER',
        'WORKDIR',
        'VOLUME',
        'STOPSIGNAL',
        'ONBUILD'
    }

    def __init__(self, path):
        self.path = path
        file_name = u'Dockerfile'
        self.docker_file_path = os.path.normpath(os.path.join(self.path,
                                                              file_name))

    def assert_dockerfile_exists(self):
        if not os.path.exists(self.docker_file_path):
            raise AnsibleContainerConductorException(u"Failed to find %s",
                                                     self.docker_file_path)

    def lines_iter(self):
        '''
        Returns contents of the Dockerfile as an array, where each line in the file is an element in the array.
        :return: list
        '''
        # Convert unicode chars to string
        byte_to_string = lambda x: x.strip().decode(u'utf-8') if isinstance(x, bytes) else x.strip()

        # Read the entire contents of the Dockerfile, decoding each line, and return the result as an array
        with open(self.docker_file_path, u'r') as f:
            return itertools.imap(byte_to_string, f)

    def preparse_iter(self):
        """
        Comments can be anywhere. So break apart the Dockerfile into significant
        lines and any comments that precede them. And if a line is a carryover
        from the previous via an escaped-newline, bring the directive with it.
        """
        to_yield = {}
        last_directive = None
        lines_processed = 0
        for line in self.lines_iter():
            if not line:
                continue
            if line.startswith(u'#'):
                comment = line.lstrip('#').strip()
                # Directives have to precede any instructions
                if lines_processed == 1:
                    if comment.startswith(u'escape='):
                        self.escape_char = comment.split(u'=', 1)[1]
                        continue
                to_yield.setdefault('comments', []).append(comment)
            else:
                # last_directive being set means the previous line ended with a
                # newline escape
                if last_directive:
                    directive, payload = last_directive, line
                else:
                    directive, payload = line.split(u' ', 1)
                if line.endswith(self.escape_char):
                    payload = payload.rstrip(self.escape_char)
                    last_directive = directive
                else:
                    last_directive = None
                to_yield['directive'] = directive
                to_yield['payload'] = payload.strip()
                yield to_yield
                to_yield = {}

    def __iter__(self):
        lines = self.lines_iter()
        lines_processed = 0
        for preparsed in self.preparse_iter():
            directive = preparsed['directive']
            if directive in self.supports_json:
                try:
                    payload = json.loads(preparsed['payload'])
                except ValueError:
                    payload = preparsed['payload']
            else:
                payload = preparsed['payload']
            payload_processor = getattr(self, 'parse_%s' % (directive,))

            if directive in self.supports_env:
                if isinstance(payload, list):
                    payload = [self.do_variable_syntax_substitution(s)
                               for s in payload]
                else:
                    payload = self.do_variable_syntax_substitution(s)

            for task in payload_processor(payload,
                                          comments=preparsed.get('comments', [])):
                yield task

    PLAIN_VARIABLE_RE = ur'(?<!\\)\$(?P<var>[a-zA-Z_]\w*)'
    BRACED_VARIABLE_RE = ur'(?<!\\)\$\{(?P<var>[a-zA-Z_]\w*)\}'
    DEFAULT_VARIABLE_RE = ur'(?<!\\)\$\{(?P<var>[a-zA-Z_]\w*):(?P<plus_minus>[+-])(?P<default>\}'

    def do_variable_syntax_substitution(self, string):
        def simple_variable_sub(match_obj):
            var_name = match_obj.group('var')
            if var_name in self.environment:
                return u"{{ lookup('env', '%s') }}" % var_name
            else:
                return u"{{ %s }}" % var_name
        string = self.PLAIN_VARIABLE_RE.sub(simple_variable_sub, string)
        string = self.BRACED_VARIABLE_RE.sub(simple_variable_sub, string)

        def default_variable_sub(match_obj):
            var_name = match_obj.group('var')
            default = match_obj.group('default')
            if var_name in self.environment:
                var_string = "lookup('env', '%s')" % var_name
            else:
                var_string = var_name
            if match_obj.group('plus_minus') == '-':
                # Use the default as a default
                return u"{{ %s | default('%s') }}" % (var_string, default)
            else:
                # If the variable is defined, use the default else blank
                return u"{{ %s | defined | ternary('%s', '') }}" % (var_string,
                                                                    default)
        string = self.DEFAULT_VARIABLE_RE.sub(default_variable_sub, string)
        return string


    @staticmethod
    def _simple_meta_parser(meta_key):
        def __wrapped__(self, payload, comments):
            if isinstance(payload, list):
                self.meta[meta_key] = CommentableList(payload, comments)
            elif isinstance(payload, dict):
                self.meta[meta_key] = CommentableDict(payload, comments)
            else:
                self.meta[meta_key] = CommentableString(payload, comments)
            return []
        return __wrapped__

    parse_FROM = _simple_meta_parser('from')

    def parse_RUN(self, payload, comments):
        task = {}
        if isinstance(payload, list):
            task['command'] = subprocess.list2cmdline(payload)
        else:
            task['shell'] = payload.rstrip(u';').rstrip(u'&&')
            if self.shell:
                task['args']['executable'] = self.shell
        if comments:
            task['name'] = u' '.join(comments)
        if self.user:
            task['remote_user'] = self.user
        if self.working_dir:
            task['args']['chdir'] = self.working_dir
        return [task]

    parse_CMD = _simple_meta_parser('command')

    def parse_LABEL(self, payload, comments):
        kv_pairs = shlex.split(payload)
        for k, v in [kv.split('=', 1) for kv in kv_pairs]:
            self.meta.setdefault('labels', CommentableDict({}, comments))[k] = v
        return []

    parse_MAINTAINER = _simple_meta_parser('maintainer')

    def parse_EXPOSE(self, payload, comments):
        ports = payload.split(' ')
        self.meta.setdefault('ports', CommentableList([], comments)).extend(ports)
        return []

    def parse_ENV(self, payload, comments):
        kv_parts = shlex.split(payload)
        # It's possible this is a single environment variable being set using
        # the syntax that doesn't require an = sign
        if len(kv_parts) == 2 and u'=' not in payload:
            k, v = kv_parts
            self.meta.setdefault('environment', {})[k] = CommentableString(v, comments)
        else:
            kv_dict = {k: v for k, v in [part.split(u'=', 1) for part in kv_parts]}
            self.meta.setdefault('environment', CommentableDict({}, comments)).update(kv_dict)
        return []

    def parse_ADD(self, payload, comments, url_and_tarball=True):
        if isinstance(payload, list):
            dest = payload.pop()
            src_list = payload
        else:
            _src, dest = payload.split(u' ', 1)
            src_list = [_src]

        tasks = []
        # ADD ensures the dest path exists
        dest_path = os.path.dirname(dest)
        tasks.append(
            {'name': u'Ensure %s exists' % dest_path,
             'file': {'path': dest_path,
                      'state': 'directory'}}
        )

        for src_spec in src_list:
            # ADD src can be a URL - look for a scheme
            if url_and_tarball and urlparse.urlparse(src_spec).scheme in ['http', 'https']:
                task = {'get_url': {'url': src_spec, 'dest': dest, 'mode': 0600}}
                if comments:
                    task['name'] = u' '.join(comments)
                tasks.append(task)
            else:
                real_path = os.path.join(self.path, src_spec)
                if url_and_tarball:
                    # ADD src can be a tarfile
                    try:
                        _ = tarfile.open(real_path, mode='r:*')
                    except tarfile.ReadError:
                        # Not a tarfile.
                        task = {'copy': {'src': src_spec,
                                         'dest': dest}}
                        if comments:
                            task['name'] = u' '.join(comments)
                        tasks.append(task)
                else:
                    # path specifiers can be fnmatch expressions, so use glob
                    for abs_src in glob.iglob(real_path):
                        src = os.path.relpath(abs_src, self.path)

                        if os.path.isdir(real_path):
                            task = {'synchronize': {'src': src,
                                                    'dest': dest,
                                                    'recursive': 'yes'}}
                        elif os.path.isfile(real_path):
                            task = {'unarchive': {'src': src,
                                                  'dest': dest}}
                        else:
                            continue
                    if comments:
                        task['name'] = u' '.join(comments + [u'(%s)' % src])
                    tasks.append(task)
        for task in tasks:
            if self.user:
                task['remote_user'] = self.user
        return tasks

    def parse_COPY(self, payload, comments):
        return self.parse_ADD(payload, comments=comments, url_and_tarball=False)

    parse_ENTRYPOINT = _simple_meta_parser('entrypoint')

    def parse_VOLUME(self, payload, comments):
        if not isinstance(payload, list):
            payload = payload.split(u' ')
        self.meta['volumes'] = CommentableList(payload, comments)

    def parse_USER(self, payload, comments):
        self.meta['user'] = CommentableString(payload, comments)
        self.user = payload
        return []

    def parse_WORKDIR(self, payload, comments):
        self.meta['working_dir'] = CommentableString(payload, comments)
        self.working_dir = payload
        return []

    def parse_ARG(self, payload, comments):
        # ARG can either be set with a default or not
        if u'=' in payload:
            arg, default = payload.split(u'=', 1)
        else:
            arg, default = payload, u'~'
        self.variables[arg] = CommentableString(default, comments)

    parse_ONBUILD = _simple_meta_parser('onbuild')

    parse_STOPSIGNAL = _simple_meta_parser('stop_signal')

    parse_HEALTHCHECK = _simple_meta_parser('healthcheck')

    def parse_SHELL(self, payload, comments):
        self.meta['shell'] = CommentableList(payload, comments)
        self.shell = payload
        return []


class DockerfileImport(object):
    project_name = None
    import_from = None
    base_path = None

    def __init__(self, base_path, project_name, import_from):
        # The path to write the Ansible Container project to
        self.base_path = base_path
        # The name of the Ansible Container project
        self.project_name = project_name
        # The path containing the Dockerfile and build context
        self.import_from = import_from


    @property
    def role_path(self):
        return os.path.join(self.base_path, 'roles', self.project_name)

    def create_role_template(self):
        '''
        Create roles/dockerfile-to-ansible path and role structure.
        :return: None
        '''
        description = u"Imported Dockerfile for {}".format(self.project_name)

        create_role_from_templates(role_name=self.project_name,
                                   role_path=self.role_path,
                                   project_name=self.project_name,
                                   description=description)

    def add_role_tasks(self):
        '''
        Evaluates self.instructions, and transforms any RUN and ADD tasks to the role's tasks/main.yml
        :return: None
        '''

        def get_run_tasks(instruction):
            '''
            Transforms a dockerfile RUN command into a set of playbook tasks.

            :param instruction: dict containing RUN command attributes
            :return: list of dicts, where each represents a task
            '''

            def create_task(cmd, preceding_comments):
                task = CommentedMap()
                name = ''
                if preceding_comments:
                    # If there are preceding comment lines, use the first as the task name.
                    name = preceding_comments[0]
                if re.search(ur'[><|]', cmd):
                    # If the command includes a pipe, it's *generally* better to use the shell module
                    task[u'name'] = u'Shell command' if not name else name
                    task[u'shell'] = cmd
                else:
                    # Otherwise, the command module *should* work
                    task[u'name'] = u'Command command' if not name else name
                    task[u'command'] = cmd
                if preceding_comments and len(preceding_comments) > 1:
                    # When multiple preceding comment lines, place them before the task name
                    task.yaml_set_comment_before_after_key('name', before=u'\n'.join(preceding_comments), indent=2)
                return task

            run_tasks = []

            if isinstance(instruction[u'value'], list):
                count = 0
                for value in instruction[u'value']:
                    if count == 0:
                        # Pass all preceding comments on first instruction
                        comments = instruction[u'preceding_comments']
                    else:
                        # Only pass the first comment to use as 'name' value on subsequent instructions
                        comments = [instruction[u'preceding_comments'][0]] if instruction[u'preceding_comments'] else []
                    run_tasks.append(create_task(value, comments))
                    count += 1
            else:
                run_tasks.append(create_task(instruction[u'value'], instruction[u'preceding_comments']))
            return run_tasks

        def get_add_tasks(instruction):
            '''
            Transforms a dockerfile ADD command into a playbook task.

            :param instruction: dict containing ADD command attributes
            :return: dict containing task attributes
            '''
            if isinstance(instruction[u'value'], list):
                raise NotImplementedError(u'Error: expected the ADD command to be a string.')

            preceding_comments = instruction['preceding_comments']
            name = ''
            if preceding_comments:
                # If there are preceding comments, use the first as the task name
                name = preceding_comments[0]

            add_tasks = []
            task = CommentedMap()
            src, dest = instruction[u'value'].split(' ')
            src_path = os.path.normpath(os.path.join(u'../../', src))

            # TODO: The dest directory may contain environment vars, and it may also be relavant to WORKDIR.
            #       Modify the following to use environment_vars and workdir properties to resolve the full
            #       dest path.

            if os.path.isdir(os.path.join(self.base_path, src)):
                task[u'name'] = name if name else u'Synch {}'.format(src)
                task[u'synchronize'] = CommentedMap()
                task[u'synchronize'][u'src'] = src_path
                task[u'synchronize'][u'dest'] = dest
            elif re.search(ur'[*?]', src):
                task[u'name'] = name if name else u'Synch {}'.format(src)
                task[u'synchronize'] = CommentedMap()
                task[u'synchronize'][u'src'] = u"{{ item }}"
                task[u'synchronize'][u'dest'] = dest
                task[u'with_items'] = list(src_path)
            else:
                task[u'name'] = name if name else u'Copy {}'.format(src)
                task[u'copy'] = CommentedMap()
                task[u'copy'][u'src'] = src_path
                task[u'copy'][u'dest'] = dest

            if instruction['inline_comment']:
                task.yaml_add_eol_comment(instruction['inline_comment'], key='name')

            if preceding_comments and len(preceding_comments) > 1:
                # When multiple preceding comment lines, place them before the task name.
                task.yaml_set_comment_before_after_key('name', before=u'\n'.join(preceding_comments), indent=2)

            add_tasks.append(task)
            return add_tasks

        tasks = []
        logger.debug(json.dumps(self.instructions, indent=4))
        for instruction in self.instructions:
            if instruction[u'command'] == u'RUN':
                tasks += get_run_tasks(instruction)
            elif instruction[u'command'] == u'ADD':
                tasks += get_add_tasks(instruction)

        main_yml = os.path.normpath(os.path.join(self.role_path, u'tasks', u'main.yml'))
        try:
            task_yaml = ruamel.yaml.dump(tasks,
                                         Dumper=ruamel.yaml.RoundTripDumper,
                                         default_flow_style=False,
                                         )
        except Exception:
            raise AnsibleContainerConductorException(u'Error: Failed to write %s', main_yml)

        with open(main_yml, u'w') as f:
            f.write(re.sub(ur'^-', u'\n-', task_yaml, flags=re.M))

    def create_container_yaml(self):
        pass
        # def get_directives(instruction):
        #     '''
        #     Transforms Dockerfile commands into container.yml directives.
        #
        #     :param instruction: dict containing RUN command attributes
        #     :return: list of dicts, where each represents a task
        #     '''
        #
        #     def create_task(cmd, preceding_comments):
        #         task = CommentedMap()
        #         name = ''
        #         if preceding_comments:
        #             # If there are preceding comment lines, use the first as the task name.
        #             name = preceding_comments[0]
        #         if re.search(ur'[><|]', cmd):
        #             # If the command includes a pipe, it's *generally* better to use the shell module
        #             task[u'name'] = u'Shell command' if not name else name
        #             task[u'shell'] = cmd
        #         else:
        #             # Otherwise, the command module *should* work
        #             task[u'name'] = u'Command command' if not name else name
        #             task[u'command'] = cmd
        #         if preceding_comments and len(preceding_comments) > 1:
        #             # When multiple preceding comment lines, place them before the task name
        #             task.yaml_set_comment_before_after_key('name', before=u'\n'.join(preceding_comments), indent=2)
        #         return task
        #
        #     run_tasks = []
        #
        #     if isinstance(instruction[u'value'], list):
        #         count = 0
        #         for value in instruction[u'value']:
        #             if count == 0:
        #                 # Pass all preceding comments on first instruction
        #                 comments = instruction[u'preceding_comments']
        #             else:
        #                 # Only pass the first comment to use as 'name' value on subsequent instructions
        #                 comments = [instruction[u'preceding_comments'][0]] if instruction[u'preceding_comments'] else []
        #             run_tasks.append(create_task(value, comments))
        #             count += 1
        #     else:
        #         run_tasks.append(create_task(instruction[u'value'], instruction[u'preceding_comments']))
        #     return run_tasks
        #
        # tasks = []
        # logger.debug(json.dumps(self.instructions, indent=4))
        # for instruction in self.instructions:
        #     if instruction[u'command'] not in [u'RUN', u'ADD']:
        #         tasks += get_run_tasks(instruction)
        #
        # main_yml = os.path.normpath(os.path.join(self.role_path, u'tasks', u'main.yml'))
        # try:
        #     task_yaml = ruamel.yaml.dump(tasks,
        #                                  Dumper=ruamel.yaml.RoundTripDumper,
        #                                  default_flow_style=False,
        #                                  )
        # except Exception:
        #     raise AnsibleContainerException(u'Error: Failed to write {}'.format(main_yml))
        #
        # with open(main_yml, u'w') as f:
        #     f.write(re.sub(ur'^-', u'\n-', task_yaml, flags=re.M))


def yscbak(self, key, before=None, indent=0, after=None, after_indent=None):
    """
    Expects comment (before/after) to be without `#`, and possibly have multiple lines

    Adapted from:
      http://stackoverflow.com/questions/40704916/how-to-insert-a-comment-line-to-yaml-in-python-using-ruamel-yaml

    This code seems to be in the code base, but has not made it into a release:
      https://bitbucket.org/ruamel/yaml/src/46689251cce58a4331a8674d14ef94c6db1e96e2/comments.py?at=default&fileviewer=file-view-default#comments.py-201

    """

    def comment_token(s, mark):
        # handle empty lines as having no comment
        return CommentToken(('# ' if s else '') + s + '\n', mark, None)

    if after_indent is None:
        after_indent = indent + 2
    if before and before[-1] == '\n':
        before = before[:-1]  # strip final newline if there
    if after and after[-1] == '\n':
        after = after[:-1]  # strip final newline if there
    start_mark = Mark(None, None, None, indent, None, None)
    c = self.ca.items.setdefault(key, [None, [], None, None])
    if before:
        for com in before.split('\n'):
            c[1].append(comment_token(com, start_mark))
    if after:
        start_mark = Mark(None, None, None, after_indent, None, None)
        if c[3] is None:
            c[3] = []
        for com in after.split('\n'):
            c[3].append(comment_token(com, start_mark))
