"""sc-githooks - Git routines

Copyright (c) 2021 Scott Lau
Portions Copyright (c) 2021 InnoGames GmbH
Portions Copyright (c) 2021 Emre Hasegeli
"""
import logging

from os.path import isabs, join as joinpath, normpath
from subprocess import check_output

from githooks.utils import get_exe_path, get_extension, decode_str

git_exe_path = get_exe_path('git')


class CommitList(list):
    """Routines on a list of sequential commits"""
    ref_path = None

    def __init__(self, other, branch_name):
        super(CommitList, self).__init__(other)
        self.branch_name = branch_name

    def __str__(self):
        name = '{}..{}'.format(self[0], self[-1])
        if self.ref_path:
            name += ' ({})'.format(self.branch_name)
        return name


class Commit(object):
    """Routines on a single commit"""
    null_commit_id = '0000000000000000000000000000000000000000'

    def __init__(self, commit_id, commit_list=None):
        self.commit_id = commit_id
        self.commit_list = commit_list
        self.content_fetched = False
        self.project_fetched = False
        self.changed_files = None
        self.binary_files = None

    def __str__(self):
        return self.commit_id[:8]

    def __bool__(self):
        return self.commit_id != Commit.null_commit_id

    def __nonzero__(self):
        return self.__bool__()

    def __eq__(self, other):
        return isinstance(other, Commit) and self.commit_id == other.commit_id

    def get_new_commit_list(self, branch_name):
        """Get the list of parent new commits in order"""
        output = decode_str(check_output([
            git_exe_path,
            'rev-list',
            self.commit_id,
            '--not',
            '--all',
            '--reverse',
        ]))
        commit_list = CommitList([], branch_name)
        for commit_id in output.splitlines():
            commit = Commit(commit_id, commit_list)
            commit_list.append(commit)
        return commit_list

    def _fetch_content(self):
        content = check_output(
            [git_exe_path, 'cat-file', '-p', self.commit_id]
        )
        self._parents = []
        self._message_lines = []
        # The commit message starts after the empty line.  We iterate until
        # we find one, and then consume the rest as the message.
        lines = iter(content.splitlines())
        for line in lines:
            if not line:
                break
            if line.startswith(b'parent '):
                self._parents.append(Commit(line[len(b'parent '):].rstrip()))
            elif line.startswith(b'author '):
                self._author = Contributor.parse(line[len(b'author '):])
            elif line.startswith(b'committer '):
                self._committer = Contributor.parse(line[len(b'committer '):])
        for line in lines:
            self._message_lines.append(decode_str(line))
        self.content_fetched = True

    def _get_project_info(self):
        content = check_output(
            [git_exe_path, 'remote', '-v']
        )
        self._projects = None
        lines = iter(content.splitlines())
        temp = [str(_, encoding='utf-8') for _ in lines if "push" in str(_, encoding='utf-8')][0]
        line = temp.replace(" ", "\t").split("\t")[1]
        project_name = line.split("/")[-1]
        self._projects = project_name.split(".")[0]
        self.project_fetched = True

    def get_projects(self):
        if not self.project_fetched:
            self._get_project_info()
        return self._projects

    def get_parents(self):
        if not self.content_fetched:
            self._fetch_content()
        return self._parents

    def get_author(self):
        if not self.content_fetched:
            self._fetch_content()
        return self._author

    def get_committer(self):
        if not self.content_fetched:
            self._fetch_content()
        return self._committer

    def get_contributors(self):
        yield self.get_author()
        yield self._committer

    def get_message_lines(self):
        if not self.content_fetched:
            self._fetch_content()
        return self._message_lines

    def get_summary(self):
        return self.get_message_lines()[0]

    def parse_tags(self):
        tags = []
        rest = self.get_summary()
        while rest.startswith('[') and ']' in rest:
            end_index = rest.index(']')
            tags.append(rest[1:end_index])
            rest = rest[end_index + 1:]
        return tags, rest

    def content_can_fail(self):
        return not any(
            t in ['HOTFIX', 'MESS', 'TEMP', 'WIP']
            for t in self.parse_tags()[0]
        )

    def get_changed_files(self):
        """Return the list of added or modified files on a commit"""
        if self.changed_files is None:
            output = decode_str(check_output([
                git_exe_path,
                'diff-tree',
                '-r',
                '--root',               # Get the initial commit as additions
                '--no-commit-id',       # We already know the commit id.
                '--break-rewrites',     # Get rewrites as additions
                '--no-renames',         # Get renames as additions
                '--diff-filter=AM',     # Only additions and modifications
                self.commit_id,
            ]))
            changed_files = []
            for line in output.splitlines():
                line_split = line.split(None, 5)
                assert len(line_split) == 6
                assert line_split[0].startswith(':')
                file_mode = line_split[1]
                # sc add object_id
                object_id = line_split[3]
                file_path = line_split[5]
                changed_files.append(CommittedFile(file_path, self, file_mode, object_id))
            self.changed_files = changed_files
        return self.changed_files

    def get_binary_files(self):
        """Return the binary files on a commit"""
        if self.binary_files is None:
            output = decode_str(check_output([
                git_exe_path,
                'log',
                '--pretty=format:%H -M100%',   # pretty format
                '--numstat',       # number state of the file
                '--no-commit-id',       # We already know the commit id.
                '--break-rewrites',     # Get rewrites as additions
                '--no-renames',         # Get renames as additions
                '--diff-filter=AM',     # Only additions and modifications
                "{}^!".format(self.commit_id),
            ]))
            binary_files = []
            for line in output.splitlines():
                line_split = line.split('\t')
                if len(line_split) == 3:
                    if "-" == line_split[0] and "-" == line_split[1]:
                        binary_files.append(line_split[2])
            self.binary_files = binary_files
        return self.binary_files


class Contributor(object):
    """Routines on contribution properties of a commit"""

    def __init__(self, name, email, timestamp):
        self.name = name
        self.email = email
        self.timestamp = timestamp

    @classmethod
    def parse(cls, line):
        """Parse the contribution line as bytes"""
        name, line = line.split(b' <', 1)
        email, line = line.split(b'> ', 1)
        timestamp, line = line.split(b' ', 1)
        return cls(decode_str(name), decode_str(email), int(timestamp))

    def get_email_domain(self):
        return self.email.split('@', 1)[-1]


class CommittedFile(object):
    """Routines on a single committed file"""

    def __init__(self, path, commit=None, mode=None, object_id=None):
        self.path = path
        self.commit = commit
        assert mode is None or len(mode) == 6
        self.mode = mode
        self.content = None
        # sc add object id
        self.object_id = object_id
        self.project_fetched = False

    def __str__(self):
        return '文件 {} 位于提交 {}'.format(self.path, self.commit)

    def __eq__(self, other):
        return (
            isinstance(other, CommittedFile) and
            self.path == other.path and
            self.commit == other.commit
        )

    def _get_project_info(self):
        content = check_output(
            [git_exe_path, 'remote', '-v']
        )
        self._projects = None
        lines = iter(content.splitlines())
        temp = [str(_, encoding='utf-8') for _ in lines if "push" in str(_, encoding='utf-8')][0]
        line = temp.replace(" ", "\t").split("\t")[1]
        project_name = line.split("/")[-1]
        self._projects = project_name.split(".")[0]
        self.project_fetched = True

    def get_projects(self):
        if not self.project_fetched:
            self._get_project_info()
        return self._projects

    def exists(self):
        return bool(check_output([
            git_exe_path,
            'ls-tree',
            '--name-only',
            '-r',
            self.commit.commit_id,
            self.path,
        ]))

    def changed(self):
        return self in self.commit.get_changed_files()

    def regular(self):
        return self.mode[:2] == '10'

    def symlink(self):
        return self.mode[:2] == '12'

    def owner_can_execute(self):
        owner_bits = int(self.mode[-3])
        return bool(owner_bits & 1)

    def get_object_id(self):
        return self.object_id

    def get_filename(self):
        return self.path.rsplit('/', 1)[-1]

    def get_path(self):
        return self.path.rsplit('/', 1)[-1]

    def get_framework(self):
        return False if '.framework' in self.path else True

    def get_file_size(self):
        try:
            output = check_output([
                git_exe_path,
                'cat-file',
                '-s',  # show file size
                self.object_id,
            ])
        except Exception as e:
            logging.info("get_file_size Error: %s" % e)
            output = -1
        return int(output)

    def get_extension(self):
        return get_extension(self.path)

    def get_content(self):
        """Get the file content as binary"""
        if self.content is None:
            self.content = check_output([
                git_exe_path, 'show', self.commit.commit_id + ':' + self.path
            ])
        return self.content

    def get_shebang(self):
        """Get the shebang from the file content"""
        if not self.regular():
            return None
        content = self.get_content()
        if not content.startswith(b'#!'):
            return None
        content = content[len(b'#!'):].strip()
        return decode_str(content.split(None, 1)[0])

    def get_shebang_exe(self):
        """Get the executable from the shebang"""
        shebang = self.get_shebang()
        if not shebang:
            return None
        if shebang == '/usr/bin/env':
            rest = self.get_content().splitlines()[0][len(b'#!/usr/bin/env'):]
            rest_split = rest.split(None, 1)
            if rest_split:
                return decode_str(rest_split[0])
        return shebang.rsplit('/', 1)[-1]

    def get_symlink_target(self):
        """Get the symlink target as same kind of instance

        We just return None, if the target has no chance to be on
        the repository."""
        content = self.get_content()
        if isabs(content):
            return None
        path = normpath(joinpath(self.path, '..', decode_str(content)))
        if path.startswith('..'):
            return None
        return type(self)(path, self.commit)
