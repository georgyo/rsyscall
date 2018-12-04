#define _GNU_SOURCE
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <err.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/un.h>

char *make_private_dir() {
    char *tmpdir;
    tmpdir = getenv("XDG_RUNTIME_DIR");
    if (!tmpdir) tmpdir = getenv("TMPDIR");
    if (!tmpdir) tmpdir = "/tmp";
    char *template;
    if (asprintf(&template, "%s/XXXXXX", tmpdir) < 0) {
	err(1, "asprintf");
    };
    char *dirname = mkdtemp(template);
    if (!dirname) {
	err(1, "mkdtemp");
    }
    return dirname;
}

int listen_unix_socket(int dirfd, const char *name) {
    struct sockaddr_un addr = { .sun_family = AF_UNIX, .sun_path = {} };
    int ret = snprintf(addr.sun_path, sizeof(addr.sun_path), "/proc/self/fd/%d/%s", dirfd, name);
    if (ret < 0) {
	err(1, "snprintf");
    }
    int sockfd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sockfd < 0) {
	err(1, "socket");
    }
    if (bind(sockfd, &addr, sizeof(addr)) < 0) {
	err(1, "bind");
    }
    if (listen(sockfd, 10) < 0) {
	err(1, "listen");
    }
    return sockfd;
}

int main()
{
    char *dir = make_private_dir();
    const int dirfd = open(dir, O_DIRECTORY|O_CLOEXEC);
    if (dirfd < 0) err(1, "open");
    const int datasock = listen_unix_socket(dirfd, "data");
    dprintf(1, "%s/data\n", dir);
    const int passsock = listen_unix_socket(dirfd, "pass");
    dprintf(1, "%s/pass\n", dir);
    dprintf(1, "done\n");
    if (close(1) < 0) err(1, "close(1)");
    const int connsock = accept4(passsock, NULL, NULL, SOCK_CLOEXEC);
    if (connsock < 0) err(1, "accept4(passsock)");
    if (close(passsock) < 0) err(1, "close(passsock)");
    if (unlinkat(dirfd, "pass", 0) < 0) err(1, "unlinkat");
    union {
        struct cmsghdr hdr;
        char buf[CMSG_SPACE(sizeof(int))];
    } cmsg = {
        .hdr = {
            .cmsg_len = CMSG_LEN(sizeof(int)),
            .cmsg_level = SOL_SOCKET,
            .cmsg_type = SCM_RIGHTS,
        },
    };
    *((int *) CMSG_DATA(&cmsg.hdr)) = datasock;
    char waste_data = 0;
    struct iovec io = {
        .iov_base = &waste_data,
        .iov_len = sizeof(waste_data),
    };
    struct msghdr msg = {
        .msg_name = NULL,
        .msg_namelen = 0,
        .msg_iov = &io,
        .msg_iovlen = 1,
        .msg_control = &cmsg,
        .msg_controllen = sizeof(cmsg),
    };
    if (sendmsg(connsock, &msg, 0) < 0) err(1, "sendmsg(connsock=%d, {msg={datasock=%d}})", connsock, datasock);
    if (close(connsock) < 0) err(1, "close(connsock)");
    free(dir);
    if (close(datasock) < 0) err(1, "close(connsock)");
    if (close(dirfd) < 0) err(1, "close(connsock)");
    return 0;
}
