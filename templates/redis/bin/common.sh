# Environment variable common to all scripts
APISERVER=https://kubernetes.default.svc
SERVICEACCOUNT=/var/run/secrets/kubernetes.io/serviceaccount
NAMESPACE=$(cat ${SERVICEACCOUNT}/namespace)
TOKEN=$(cat ${SERVICEACCOUNT}/token)
CACERT=${SERVICEACCOUNT}/ca.crt

TIMEOUT=3

POD_NAME=$HOSTNAME
POD_FQDN=$HOSTNAME.$SVC_FQDN

# Extract pod IP from /etc/hosts
POD_IP=$(grep "$HOSTNAME" /etc/hosts | awk '{print $1}' | head -1)

if test -d /var/lib/config-data/tls; then
    REDIS_CLI_CMD="redis-cli --tls"
    REDIS_CONFIG=/var/lib/redis/redis-tls.conf
    SENTINEL_CONFIG=/var/lib/redis/sentinel-tls.conf
else
    REDIS_CLI_CMD=redis-cli
    REDIS_CONFIG=/var/lib/redis/redis.conf
    SENTINEL_CONFIG=/var/lib/redis/sentinel.conf
fi

function log() {
    echo "$(date +%F_%H_%M_%S) $*"
}

function log_error() {
    echo "$(date +%F_%H_%M_%S) ERROR: $*"
}

function generate_configs() {
    # Copying config files except template files
    tar -C /var/lib/config-data --exclude '..*' --exclude '*.in' -h -c default | tar -C /var/lib/config-data/generated -x --strip=1
    # Generating config files from templates
    cd /var/lib/config-data/default
    for cfg in $(find -L * -name '*.conf.in'); do
        log "Generating config file from template $PWD/${cfg}"
        sed -e "s/{ POD_FQDN }/${POD_FQDN}/g" -e "s/{ POD_IP }/${POD_IP}/g" "${cfg}" > "/var/lib/config-data/generated/${cfg%.in}"
    done
}

function is_bootstrap_pod() {
    echo "$1" | grep -qe '-0$'
}

function extract() {
    local var="$1"
    local output="$2"
    # parse curl vars as well as kube api error fields
    echo "$output" | awk -F'[:,]' "/\"?${var}\"?:/ {print \$2; exit}"
}

function configure_pod_label() {
    local pod="$1"
    local patch="$2"
    local success="$3"
    local curlvars="\nexitcode:%{exitcode}\nerrormsg:%{errormsg}\nhttpcode:%{response_code}\n"

    response=$(curl -s -w "${curlvars}" --cacert ${CACERT} --header "Content-Type:application/json-patch+json" --header "Authorization: Bearer ${TOKEN}" --request PATCH --data "$patch" ${APISERVER}/api/v1/namespaces/${NAMESPACE}/pods/${pod})

    exitcode=$(extract exitcode "$response")
    if [ $exitcode -ne 0 ]; then
        errormsg=$(extract errormsg "$response")
        log_error "Error when running curl: ${errormsg} (${exitcode})"
        return 1
    fi

    httpcode=$(extract httpcode "$response")
    if echo "${httpcode}" | grep -v -E "^${success}$"; then
        message=$(extract message "$response")
        log_error "Error when calling API server: ${message} (${httpcode})"
        return 1
    fi
}

function remove_pod_label() {
    local pod="$1"
    local label="$2"
    local patch="[{\"op\": \"remove\", \"path\": \"/metadata/labels/${label}\"}]"
    # 200: OK, 422: not found
    configure_pod_label $pod "$patch" "(200|422)"
}

# Wait for a peer sentinel to report a valid master for the cluster.
# Contacts each peer pod individually by FQDN (skipping self) to avoid
# the headless service DNS resolving to our own uninitialized sentinel.
# If a peer still reports US as master (stale info before
# down-after-milliseconds triggers failover), keeps retrying until
# failover completes and a different master is elected.
# If no peers are reachable at all (first deployment), returns
# immediately so the bootstrap pod can start without delay.
# Prints the master address on success (FQDN or IP).
function wait_for_master() {
    local retries=${SENTINEL_RETRIES:-10}
    local delay=${SENTINEL_RETRY_DELAY:-3}
    local pod_ordinal=${POD_NAME##*-}
    local pod_base=${POD_NAME%-*}
    local replicas=${REPLICAS:-3}

    for i in $(seq 1 $retries); do
        local any_peer_reachable=0
        local ordinal=0
        while [ $ordinal -lt $replicas ]; do
            if [ "$ordinal" != "$pod_ordinal" ]; then
                local peer="${pod_base}-${ordinal}.${SVC_FQDN}"
                local output
                if output=$(timeout ${TIMEOUT} $REDIS_CLI_CMD -h ${peer} -p 26379 sentinel master redis 2>/dev/null); then
                    any_peer_reachable=1
                    local master
                    master=$(echo "$output" | awk '/^ip$/ {getline; print $0; exit}')
                    # If the peer still thinks WE are master, it has stale
                    # pre-failover info — try remaining peers before waiting.
                    if ! echo "$master" | grep -q "^${POD_NAME}\."; then
                        echo "$master"
                        return 0
                    fi
                fi
            fi
            ordinal=$((ordinal + 1))
        done
        if [ $any_peer_reachable -eq 0 ]; then
            return 1
        fi
        log "Attempt $i/$retries: no valid master found, retrying in ${delay}s..."
        sleep $delay
    done
    return 1
}

function set_pod_label() {
    local pod="$1"
    local label="$2"
    local patch="[{\"op\": \"add\", \"path\": \"/metadata/labels/${label}\", \"value\": \"true\"}]"
    # 200: OK
    configure_pod_label $pod "$patch" "200"
}
