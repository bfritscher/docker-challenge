import sys
from contextlib import closing

import docker
import docker.api.build


def create_docker_client(**options):
    tls = docker.tls.TLSConfig(
        client_cert=("/certs/client/cert.pem", "/certs/client/key.pem"),
        ca_cert="/certs/client/ca.pem",
        verify=False,
    )
    return docker.DockerClient(base_url="tcp://dind:2376", tls=tls, **options)


if __name__ == "__main__":
    context, tag, cache = sys.argv[1:]
    # Embed the submitted Dockerfile in the trusted context without writing it to disk.
    docker.api.build.process_dockerfile = lambda dockerfile, path: ("Dockerfile", dockerfile)
    try:
        with closing(create_docker_client(timeout=None)) as client:
            client.images.build(
                path=context,
                dockerfile=sys.stdin.buffer.read().decode("utf-8"),
                tag=tag,
                nocache=cache == "no-cache",
                forcerm=True,
            )
    except Exception as error:
        raise SystemExit(str(error)) from error
