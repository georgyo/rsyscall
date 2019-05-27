"""Functions and classes for a connection between two threads, with which we can open channels for data transfer
"""
from __future__ import annotations
import abc
import typing as t
import trio
from rsyscall.epoller import AsyncFileDescriptor, Epoller, EpollThread
from rsyscall.handle import FileDescriptor, WrittenPointer, Task
from rsyscall.memory.ram import RAM
from rsyscall.concurrency import make_n_in_parallel

from rsyscall.sys.socket import AF, SOCK, Address, SendmsgFlags, RecvmsgFlags, SendMsghdr, RecvMsghdr, CmsgList, CmsgSCMRights, Socketpair
from rsyscall.sys.uio import IovecList
from rsyscall.fcntl import F, O

class Connection:
    """A connection between two threads in which more channels can be opened

    This is not necessarily a connection in the style of TCP as such; it merely
    represents that there is a way to open channels between two threads, not
    that there is any active transfer between them at the moment.

    In terms of TCP/IP, the Connection might represent that the two threads are
    on the same network, and that new TCP connections can be initiated between
    them, but doesn't represent a specific TCP connection.

    Of course, with more flexible systems like QUIC or SCTP, the Connection
    would indeed represent an established, heartbeating connection; new channels
    for data transfer can be established in such systems given an existing
    network-level connection.

    """
    @abc.abstractmethod
    async def open_async_channels(self, count: int) -> t.List[t.Tuple[AsyncFileDescriptor, FileDescriptor]]: ...
    async def open_async_channel(self) -> t.Tuple[AsyncFileDescriptor, FileDescriptor]:
        [pair] = await self.open_async_channels(1)
        return pair

    @abc.abstractmethod
    async def open_channels(self, count: int) -> t.List[t.Tuple[FileDescriptor, FileDescriptor]]: ...
    async def open_channel(self) -> t.Tuple[FileDescriptor, FileDescriptor]:
        [pair] = await self.open_channels(1)
        return pair

    @abc.abstractmethod
    async def prep_fd_transfer(self) -> t.Tuple[FileDescriptor, t.Callable[[Task, RAM, FileDescriptor], Connection]]:
        """Prepare to transfer this Connection to another task; call the callable to execute

        The user should use whatever means to transport the returned file descriptor to
        the other task, then call the callable with the appropriate other task, a RAM for
        it, and the transferred file descriptor.

        The user might do this through fd inheritance, or maybe passing the fd over a Unix
        socket with SCM_RIGHTS.

        This is an async method because the connection might need to allocate some
        resources to do this.

        """
        pass

    @abc.abstractmethod
    def for_task(self, task: Task, ram: RAM) -> Connection:
        "Transfer this Connection to a new task; only works if the two tasks have the same fd table"
        pass

class FDPassConnection(Connection):
    """A Connection based on using SCM_RIGHTS to pass socketpairs around

    If the two threads are in the same fd table, then we'll skip using
    SCM_RIGHTS to pass the socketpair around.

    """
    @staticmethod
    async def make(task: Task, ram: RAM, epoller: Epoller) -> FDPassConnection:
        pair = await (await task.socketpair(AF.UNIX, SOCK.STREAM, 0, await ram.malloc(Socketpair))).read()
        return FDPassConnection(task, ram, epoller, pair.first, task, ram, pair.second)

    def __init__(self, access_task: Task, access_ram: RAM, access_epoller: Epoller, access_fd: FileDescriptor,
                 task: Task, ram: RAM, fd: FileDescriptor) -> None:
        self.access_task = access_task
        self.access_ram = access_ram
        self.access_epoller = access_epoller
        self.access_fd = access_fd
        self.task = task
        self.ram = ram
        self.fd = fd

    async def move_fds(self, fds: t.List[FileDescriptor]) -> t.List[FileDescriptor]:
        "Move the passed-in file descriptors from self.access_task to self.task"
        if self.access_task.fd_table == self.task.fd_table:
            return [fd.move(self.task) for fd in fds]
        async def sendmsg_op(sem: RAM) -> WrittenPointer[SendMsghdr]:
            iovec = await sem.ptr(IovecList([await sem.malloc(bytes, 1)]))
            cmsgs = await sem.ptr(CmsgList([CmsgSCMRights([fd for fd in fds])]))
            return await sem.ptr(SendMsghdr(None, iovec, cmsgs))
        _, [] = await self.access_fd.sendmsg(await self.access_ram.perform_batch(sendmsg_op))
        async def recvmsg_op(sem: RAM) -> WrittenPointer[RecvMsghdr]:
            iovec = await sem.ptr(IovecList([await sem.malloc(bytes, 1)]))
            cmsgs = await sem.ptr(CmsgList([CmsgSCMRights([fd for fd in fds])]))
            return await sem.ptr(RecvMsghdr(None, iovec, cmsgs))
        _, [], hdr = await self.fd.recvmsg(await self.ram.perform_batch(recvmsg_op))
        cmsgs_ptr = (await hdr.read()).control
        if cmsgs_ptr is None:
            raise Exception("cmsgs field of header is, impossibly, None")
        [cmsg] = await cmsgs_ptr.read()
        if not isinstance(cmsg, CmsgSCMRights):
            raise Exception("expected SCM_RIGHTS cmsg, instead got", cmsg)
        passed_socks = cmsg
        for sock in fds:
            await sock.close()
        return passed_socks

    async def open_channels(self, count: int) -> t.List[t.Tuple[FileDescriptor, FileDescriptor]]:
        async def make() -> Socketpair:
            return await (await self.access_task.socketpair(
                AF.UNIX, SOCK.STREAM, 0, await self.access_ram.malloc(Socketpair))).read()
        pairs = await make_n_in_parallel(make, count)
        fds = await self.move_fds([pair.second for pair in pairs])
        return [(pair.first, fd) for pair, fd in zip(pairs, fds)]

    async def open_async_channels(self, count: int) -> t.List[t.Tuple[AsyncFileDescriptor, FileDescriptor]]:
        chans = await self.open_channels(count)
        access_socks, local_socks = zip(*chans)
        async def make_afd(sock: FileDescriptor) -> AsyncFileDescriptor:
            await sock.fcntl(F.SETFL, O.NONBLOCK)
            return await AsyncFileDescriptor.make(self.access_epoller, self.access_ram, sock)
        async_access_socks = [await make_afd(sock) for sock in access_socks]
        return list(zip(async_access_socks, local_socks))

    async def prep_fd_transfer(self) -> t.Tuple[FileDescriptor, t.Callable[[Task, RAM, FileDescriptor], FDPassConnection]]:
        return self.fd, self.for_task_with_fd

    def for_task_with_fd(self, task: Task, ram: RAM, fd: FileDescriptor) -> FDPassConnection:
        return FDPassConnection(
            self.access_task,
            self.access_ram,
            self.access_epoller,
            self.access_fd,
            task, ram, fd)

    def for_task(self, task: Task, ram: RAM) -> FDPassConnection:
        return self.for_task_with_fd(task, ram, self.fd.for_task(task))

class ListeningConnection(Connection):
    """A Connection based on an (address, listening socket) pair
    
    """
    def __init__(self,
                 access_task: Task,
                 access_ram: RAM,
                 access_epoller: Epoller,
                 access_address: WrittenPointer[Address],
                 task: Task,
                 ram: RAM,
                 listening_fd: AsyncFileDescriptor,
    ) -> None:
        self.access_task = access_task
        self.access_ram = access_ram
        self.access_epoller = access_epoller
        self.access_address = access_address
        self.task = task
        self.ram = ram
        self.listening_fd = listening_fd

    async def open_async_channel(self) -> t.Tuple[AsyncFileDescriptor, FileDescriptor]:
        access_sock = await AsyncFileDescriptor.make(
            self.access_epoller, self.access_ram,
            await self.access_task.socket(self.access_address.value.family, SOCK.STREAM|SOCK.NONBLOCK))
        await access_sock.connect(self.access_address)
        sock = await self.listening_fd.accept()
        return access_sock, sock

    async def open_async_channels(self, count: int) -> t.List[t.Tuple[AsyncFileDescriptor, FileDescriptor]]:
        return await make_n_in_parallel(self.open_async_channel, count)

    async def open_channel(self) -> t.Tuple[FileDescriptor, FileDescriptor]:
        access_sock = await self.access_task.socket(self.access_address.value.family, SOCK.STREAM)
        # TODO this connect should really be async
        # but, since we're just connecting to a unix socket, it's fine I guess.
        await access_sock.connect(self.access_address)
        sock = await self.listening_fd.accept()
        return access_sock, sock

    async def open_channels(self, count: int) -> t.List[t.Tuple[FileDescriptor, FileDescriptor]]:
        return await make_n_in_parallel(self.open_channel, count)

    async def prep_fd_transfer(self) -> t.Tuple[FileDescriptor, t.Callable[[Task, RAM, FileDescriptor], Connection]]:
        return self.listening_fd.handle, self.for_task_with_fd

    def for_task_with_fd(self, task: Task, ram: RAM, fd: FileDescriptor) -> ListeningConnection:
        return ListeningConnection(
            self.access_task,
            self.access_ram,
            self.access_epoller,
            self.access_address,
            task, ram, self.listening_fd.with_handle(fd),
        )

    def for_task(self, task: Task, ram: RAM) -> ListeningConnection:
        return self.for_task_with_fd(task, ram, self.listening_fd.handle.for_task(task))

class ConnectionThread(EpollThread):
    def __init__(self,
                 task: Task,
                 ram: RAM,
                 epoller: Epoller,
                 connection: Connection,
    ) -> None:
        super().__init__(task, ram, epoller)
        self.connection = connection

    def _init_from(self, thr: ConnectionThread) -> None: # type: ignore
        super().__init__(thr.task, thr.ram, thr.epoller)
        self.connection = thr.connection

    async def open_async_channels(self, count: int) -> t.List[t.Tuple[AsyncFileDescriptor, FileDescriptor]]:
        return (await self.connection.open_async_channels(count))

    async def open_channels(self, count: int) -> t.List[t.Tuple[FileDescriptor, FileDescriptor]]:
        return (await self.connection.open_channels(count))

