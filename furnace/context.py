#
# Copyright (c) 2016-2017 Balabit
#
# This file is part of Furnace.
#
# Furnace is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 2.1 of the License, or
# (at your option) any later version.
#
# Furnace is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Furnace.  If not, see <http://www.gnu.org/licenses/>.
#

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Union

from . import pid1
from .config import NAMESPACES
from .libc import unshare, setns, CLONE_NEWPID
from .utils import PathEncoder

logger = logging.getLogger(__name__)


class ContainerPID1Manager:
    def __init__(self, root_dir: Path, *, isolate_networking=False):
        self.root_dir = root_dir.resolve()
        self.isolate_networking = isolate_networking

    def do_exec(self, control_read, control_write):
        logger.debug("Executing {} {}".format(sys.executable, pid1.__file__))
        params = json.dumps({
            "loglevel": logging.getLevelName(logger.getEffectiveLevel()),
            "root_dir": self.root_dir,
            "control_read": control_read,
            "control_write": control_write,
            "isolate_networking": self.isolate_networking,
        }, cls=PathEncoder)

        os.execl(sys.executable, sys.executable, pid1.__file__, params)

    def wait_for_ready_signal(self):
        if os.read(self.control_read, 3) != b"RDY":
            raise RuntimeError("Container PID 1 did not send Ready signal")

    def start(self):
        pipe_parent_read, pipe_child_write = os.pipe()
        pipe_child_read, pipe_parent_write = os.pipe()
        os.set_inheritable(pipe_child_read, True)
        os.set_inheritable(pipe_child_write, True)

        # We unshare (change) the pid namespace here, and other namespaces after
        # the exec, because if we exec'd in the new mount namespace, it would open
        # files in the new namespace's root, and prevent us from umounting the old
        # root after pivot_root. Note that changing the pid namespace affects only
        # the children (namely, which namespace they will be put in). It is thread
        # safe because unshare() affects the calling thread only.
        unshare(CLONE_NEWPID)

        self.pid = os.fork()
        if not self.pid:
            # this is the child process, will turn into PID1 in the container
            try:
                # this method will NOT return
                self.do_exec(pipe_child_read, pipe_child_write)
            except BaseException as e:
                # We are the child process, do NOT run parent's __exit__ handlers
                print(e, file=sys.stderr)
                os._exit(1)

        logger.debug("Container PID1 actual PID: {}".format(self.pid))

        # Reset the pid namespace of the parent process. /proc/self/ns/pid contains
        # a reference to the original pid namespace of the thread. New child processes
        # will be placed in this pid namespace after the setns() has restored the original
        # pid namespace
        original_pidns_fd = os.open('/proc/self/ns/pid', os.O_RDONLY)
        setns(original_pidns_fd, CLONE_NEWPID)
        os.close(original_pidns_fd)

        os.close(pipe_child_read)
        os.close(pipe_child_write)
        self.control_read = pipe_parent_read
        self.control_write = pipe_parent_write
        self.wait_for_ready_signal()

    def kill(self):
        # Killing pid1 will kill every other process in the context
        # The context itself will implode without any references,
        # basically cleaning up everything
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)


class SetnsContext:
    def __init__(self, pid):
        self.pid = pid
        # we open and close the ns file descriptors in the constructor
        # and 'destructor' for two reasons:
        # - if the context is used more than one time, it saves us the file opening
        #   neither the original ns, nor the container's ns is expected to change
        #   during the lifetime of this object
        # - we have to do it in a separate step from setns() calls, because after
        #   a mount namespace change, the next open might not work
        self.orig_fds = []
        self.new_fds = []
        for ns_name, ns_flag in NAMESPACES.items():
            orig_ns_fd = os.open('/proc/self/ns/{}'.format(ns_name), os.O_RDONLY)
            new_ns_fd = os.open('/proc/{}/ns/{}'.format(self.pid, ns_name), os.O_RDONLY)
            self.orig_fds.append((orig_ns_fd, ns_flag))
            self.new_fds.append((new_ns_fd, ns_flag))

    def __del__(self):
        for fd, _ in self.orig_fds + self.new_fds:
            os.close(fd)

    def __enter__(self):
        self.orig_cwd = os.getcwd()
        try:
            for new_ns_fd, ns_flag in self.new_fds:
                setns(new_ns_fd, ns_flag)
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, type, value, traceback):
        for orig_ns_fd, ns_flag in self.orig_fds:
            setns(orig_ns_fd, ns_flag)
        os.chdir(self.orig_cwd)
        return False


class ContainerContext:
    def __init__(self, root_dir: Union[str, Path], *, isolate_networking=False):
        if not isinstance(root_dir, Path):
            root_dir = Path(root_dir)
        self.root_dir = root_dir.resolve()
        self.pid1 = ContainerPID1Manager(root_dir, isolate_networking=isolate_networking)
        self.setns_context = None

    def __enter__(self):
        self.pid1.start()
        self.setns_context = SetnsContext(self.pid1.pid)
        return self

    def __exit__(self, type, value, traceback):
        self.setns_context = None
        self.pid1.kill()
        return False

    def run(self, *args, **kwargs):
        with self.setns_context:
            return subprocess.run(*args, **kwargs)

    def Popen(self, *args, **kwargs):
        with self.setns_context:
            return subprocess.Popen(*args, **kwargs)

    def interactive_shell(self, node):
        print()
        self.run(
            ['bash', '--norc', '--noprofile', '-i'],
            env={
                'PS1': 'furnace-debug@{} \033[32m\w\033[0m # '.format(node)
            }
        )
