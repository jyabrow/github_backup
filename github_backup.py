#!/usr/bin/python

'''
Script to perform synchronous backup of an organization's github repos to a
local server.  This gives us a backup copy in case github is unreachable, and
also enables users to search across repos, which is not supported in github.

This script is intended to be run periodically via cron frequently enough to
ensure that the backup is up-to-date.
'''

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
import textwrap

import requests



LOCAL_REPODIR = './repo_backups'
TOKEN_FILE = '~/.ssh/github_api_token'
ORG_NAME = 'mycompany'


# Setup logging
LOG = logging.getLogger('github_backup_repos')
LOG.setLevel(logging.DEBUG)

# Create & configure a console handler
conh = logging.StreamHandler(sys.stdout)
format = "%(asctime)s - %(levelname)s - %(name)s.%(funcName)s - %(message)s"
conh.setFormatter(logging.Formatter(format))
LOG.addHandler(conh)
#LOG.debug("testing debug")
#LOG.info("testing info")
#LOG.error("testing error")


class GBError(Exception):
    pass



class App(object):
    '''
    Application object, implements git_bakup_repos command-line application.
    '''

    def parse_args(self):
        '''
        Parse command line arguments, setup and provide --help text
        '''
        desc = """\
        This 'backup_git_repos' command copies all an organization's Github
        repos to a local container directory.
        """
        desc = textwrap.dedent(desc)

        epilog = """\

        note:
            If the local repo container directory does not exist, this tool
            will create it.

        example:
            github_backup_repos --local-repodir /data/myrepos

        prerequisites:
            This tool needs both a GitHub oauth account token AND a GitHub-
            registered ssh key. Why both? Well, we need http API access (oauth)
            to get the organization's list of repos, and we need ssh keys to
            run secure 'git clone' and pull operations. Although it would be
            simpler, we do not use the api/http/oauth 'git clone' operation,
            which is not secure; it stores the entire URL, including oauth
            token, in the .git/config file.

            To add an ssh key, follow the procedure in this document:

            https://help.github.com/articles/generating-ssh-keys

            To add an oauth token, run the following shell command:

            curl -i -u <your_username> -d '{"scopes": ["repo"]}' \\
            https://api.github.com/authorizations
 
            Then place the token in the file ~/.ssh/github_api_token
            and set both file and parent directory to read-only owner-only.

            For more GitHub oauth API info, see the following document:

            http://developer.github.com/guides/getting-started/
        """
        epilog = textwrap.dedent(epilog)

        parser = argparse.ArgumentParser(
            description=desc,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter)

        paa = parser.add_argument

        paa('--org-name', default=ORG_NAME,
            help='GitHub organization name (default is %s)' % ORG_NAME)
        paa('--token-file', default=TOKEN_FILE,
            help='File containing GitHub oauth token (default is %s)' % TOKEN_FILE)
        paa('--local-repodir', default=LOCAL_REPODIR,
            help='Local repository container directory (default is %s)' % LOCAL_REPODIR)
        paa('--dry-run', action='store_true', default=False,
            help='Run without actually updating local repos')

        return parser.parse_args()


    def run(self):
        '''
        Runs backup of all organization's github repos to a local git repos
        contained in local_repodir.
        '''
        args = self.parse_args()
        self.org_name = args.org_name
        self.api_token = self._get_github_api_token(args.token_file)
        self.local_repodir = args.local_repodir
        self.dry_run = args.dry_run

        if self.dry_run:
            LOG.warn("dry-run mode, no changes will be made")

        repos_list = self._get_org_repos()
        self._run_backups(repos_list)

        if self.dry_run:
            LOG.warn("dry-run mode, no changes were made")


    def _get_org_repos(self):
        '''
        Gets a full list of an organization's repos from github, returns a
        list-of-dict containing full information for all repos.
        '''

        # Github api paginates query results, to get all pages, repeat query with
        # incremented page number until empty results returned.

        query_str = "https://api.github.com/orgs/%s/repos?per_page=100&page=%d"
        page_number = 1
        headers = {'authorization': 'token %s' % self.api_token}
        repos_all = []

        LOG.info("getting github repo list...")
        while True:
            query_url = query_str % (self.org_name, page_number)
            LOG.debug("request='%s'" % (query_url))
            try:
                req = requests.get(query_url, headers=headers)
                response = req.content
                status_code = req.status_code
            except Exception, msg:
                msg = "error getting repos list from github, query=%s, err=%s" % \
                      (query_url, msg)
                raise GBError, msg

            repos_page = json.loads(response)

            if status_code != 200:
                msg = "error getting repos list from github, query=%s, " \
                      "status=%s, reason=%s" % (query_url, status_code, req.reason)
                raise GBError, msg

            #TODO: repos_page should be a list of dict, if just a dict, there's an error.

            if repos_page == []:
                break

            repos_all += repos_page
            page_number += 1

        msg = "organization=%s, n_repos=%d" % (self.org_name, len(repos_all))
        LOG.debug(msg)

        return repos_all



    def _run_backups(self, repos_list):
        '''
        Processes each repo in the list received from github. Runs a 'git clone'
        if the local repo does not exist, otherwise runs a 'git pull' for each
        branch in the local repo.
        '''

        local_repodir = os.path.abspath(self.local_repodir)
        if not os.path.isdir(local_repodir):
            try:
                os.makedirs(local_repodir)
            except Exception, err:
                msg = "cannot create local_repodir=%s, %s" % (local_repodir, err)
                raise IOError, msg

        for repo in sorted(repos_list):

            repo_name = repo['name']
            full_name = repo['full_name']

            msg = ("considering remote repo %s, full_name=%s",
                   (repo_name, full_name))
            LOG.info(msg)

            ## Shell command to perform a 'git pull' on all branches of a repo
            #pab_cmd = '''\
            #git remote update; \
            #for b in `git branch -r | grep -v 'HEAD' | sed -e 's#origin/##'`; do \
            #echo pulling repo=%s branch=$b; git checkout $b && git pull; done \
            #'''
            #pab_cmd = textwrap.dedent(pab_cmd) % (repo_name)

            repo_path = local_repodir + "/" + repo_name
            try:
                if not os.path.exists(repo_path):
                    self._clone_repo_local(repo_path, full_name)
                else:
                    self._pull_all_branches(repo_name, repo_path)

            except KeyboardInterrupt:
                raise IOError, "CTRL-C received, aborting"

            except Exception, err:
                msg = "error updating repo=%s, err=%s" % (repo_name, err)
                LOG.error(msg)
                # Allow main loop to continue



    def _clone_repo_local(self, repo_path, full_name):
        '''
        Performs a 'git clone' of GitHub repo to local repo
        '''
        LOG.info("creating local_repo=%s" % (repo_path))
        local_repodir = os.path.basename(repo_path)
        os.chdir(local_repodir)  # Go (back) to container directory
        LOG.info("current_directory=%s" % (os.getcwd()))
        cmd = "git clone git@github.com:%s.git" % (full_name)
        self._run_system_cmd_nb(cmd)
        os.chdir(repo_path)    # Get into repo directory



    def _pull_all_branches(self, repo_name, repo_path):
        '''
        Performs a 'git pull' on all branches of a Github repo
        '''
        LOG.info("updating local_repo=%s, all branches" % (repo_path))
        os.chdir(repo_path)
        LOG.info("current_directory=%s" % (os.getcwd()))

        gab_cmd = 'git branch -r'
        all_branches = self._run_system_cmd_nb(gab_cmd, stdoutctl='return')

        for curr_branch in all_branches:
            cbranch = curr_branch[len('  origin'):]    # Remove prefix
            if 'origin/HEAD' in cbranch:               # Skip HEAD branch entry
                continue
            LOG.info("updating repo=%s, branch=%s" % (repo_name, curr_branch))
            gcb_cmd = "git branch %s && git pull" % (curr_branch)
            self._run_system_cmd_nb(gcb_cmd)



    def _run_system_cmd(self, cmd):
        '''
        Runs a system command while logging the command
        '''
        if not self.dry_run:
            LOG.info("running cmd: %s" % (cmd))
            os.system(cmd)
        return



    def _run_system_cmd_nb(self, cmd, stdoutctl='log'):
        '''
        Runs a non-blocking system command via subprocess module, asynchronously
        captures all stdout & stderr output text lines, and prints output to
        log, or returns output to calling function.

        Arguments:
            cmd - Command string or list. If single string then subprocess is
                  run with shell=True.  If list, then shell=False.
            stdoutctl - What to do with stdout, 'log' => log.info, 'return' =>
                        return list of output text lines to calling function.
        '''
        LOG.info("running cmd: %s" % (cmd))

        if self.dry_run:
            return

        # Non-blocking subprocess I/O method, adapted from:
        # stackoverflow.com/questions/8980050/persistent-python-subprocess

        proc = subprocess.Popen(
            cmd,
            #shell = True if isinstance(cmd, str) else False,
            shell = True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            )

        # Set output pipes to be nonblocking
        fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
        fcntl.fcntl(proc.stderr.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)

        rtn_lines = []

        # Simple polling loop to print cmd's stdout/stderr output as it appears
        while proc.poll() is None:
            try:
                output = proc.stdout.readline().rstrip()
            except IOError:
                pass    # No data to read, skip
            else:
                if stdoutctl == 'return':
                    rtn_lines.append(output)
                else:
                    LOG.info(output)

            try:
                errout = proc.stderr.readline().rstrip()
            except IOError:
                pass    # No data to read, skip
            else:
                LOG.error(errout)

            time.sleep(0.01)

        LOG.debug("proc.returncode=%d" % (proc.returncode))

        return None if stdoutctl == 'log' else rtn_lines



    def _get_github_api_token(self, github_token_file):
        '''
        Reads github (oauth) api token from the github_token_file. Also enforces
        best security practices by requiring the proper permissions for the
        token file (same as ssh key rules).  If any problem prevents reading
        the key, will raise an IOError exception.
        '''
        token_file = os.path.expanduser(github_token_file)

        # Verify file exists
        if not os.path.exists(token_file):
            msg = "missing file %s, cannot proceed" % (token_file)
            raise IOError, msg

        # Check permissions on the parent directory
        token_parent_dir = os.path.dirname(token_file)
        perms = os.stat(token_parent_dir).st_mode & 0777
        if perms & 0077 != 0:
            msg = "bad permissions mode (%o) on directory %s, must be " \
                  "owner-access-only (e.g. 0700)" % (perms, token_parent_dir)
            raise IOError, msg

        # Check permissions on the file itself
        perms = os.stat(token_file).st_mode & 0777
        if perms & 0077 != 0:
            msg = "bad permissions mode (%o) on file %s, must be " \
                  "owner-access-only (e.g. 0400)" % (perms, token_file)
            raise IOError, msg

        # Read file contents as the api token
        try:
            tf = open(token_file)
            github_api_token = tf.read().rstrip()
        except Exception, err:
            msg = "cannot read value from %s, err=%s" % (token_file, err)
            raise IOError, msg
        finally:
            tf.close()

        return github_api_token



    def main(self):
        '''
        Runs the application, catches exceptions and returns exit status
        '''
        try:
            self.run()
        except (IOError, GBError), err:
            msg = "github_backup_repos: %s" % err
            LOG.error(msg)
            sys.exit(1)


if __name__ == '__main__':
    myapp = App()
    myapp.main()

