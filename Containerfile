FROM python:3.14

WORKDIR /app

COPY mtv_pipelines/ ./mtv_pipelines/
COPY pyproject.toml .
COPY poetry.lock .
COPY README.md .
COPY root.pem .

RUN pip install poetry

RUN poetry install

RUN apt-get update && apt-get -y install skopeo

RUN (type -p wget >/dev/null || (apt update && apt install wget -y)) \
	&& mkdir -p -m 755 /etc/apt/keyrings \
	&& out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg \
	&& cat $out | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
	&& chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& mkdir -p -m 755 /etc/apt/sources.list.d \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
	&& apt update \
	&& apt install gh -y

# kustomize >= v5.7.1 — required by the konflux_stream pipeline's build-single.sh
RUN apt-get update && apt-get -y install curl \
	&& curl -sfL "https://raw.githubusercontent.com/kubernetes-sigs/kustomize/master/hack/install_kustomize.sh" \
	   | bash -s 5.7.1 /usr/local/bin \
	&& kustomize version

# yq (mikefarah/yq v4) — required by build-single.sh's ensure-releaseplan-authors.sh
RUN wget -qO /usr/local/bin/yq \
	"https://github.com/mikefarah/yq/releases/latest/download/yq_linux_$(dpkg --print-architecture)" \
	&& chmod +x /usr/local/bin/yq \
	&& yq --version

# oc (OpenShift CLI) — required by bundle_sync (oc get releases/snapshots).
# Must still be logged into the cluster at runtime.
RUN case "$(dpkg --print-architecture)" in \
	    amd64) oc_url="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz" ;; \
	    arm64) oc_url="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux-arm64.tar.gz" ;; \
	    *) echo "unsupported arch for oc: $(dpkg --print-architecture)" >&2; exit 1 ;; \
	  esac \
	&& curl -sfL "$oc_url" | tar -xz -C /usr/local/bin oc \
	&& oc version --client

# Go + govulncheck — required by dep_bump (go get/tidy/vendor/build).
# GOTOOLCHAIN=auto fetches a newer toolchain when a go.mod asks for one.
# libvirt-dev/pkg-config: forklift's root module links libvirt via cgo.
# binutils-gold: go passes -fuse-ld=gold on arm64; binutils 2.44 dropped it.
ENV PATH="/usr/local/go/bin:${PATH}" GOTOOLCHAIN=auto
RUN apt-get update && apt-get -y install libvirt-dev pkg-config binutils-gold \
	&& curl -sfL "https://go.dev/dl/go1.25.9.linux-$(dpkg --print-architecture).tar.gz" \
	   | tar -xz -C /usr/local \
	&& GOBIN=/usr/local/bin go install golang.org/x/vuln/cmd/govulncheck@v1.1.4 \
	&& go version && govulncheck -version
