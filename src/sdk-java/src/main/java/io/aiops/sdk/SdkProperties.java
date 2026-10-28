package io.aiops.sdk;

import java.util.Map;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

@ConfigurationProperties("aiops.sdk")
public record SdkProperties(
    @DefaultValue("true") boolean enabled,
    String observedName, String serviceName,
    @DefaultValue("unknown") String env, String cluster, String instance,
    @DefaultValue("127.0.0.1") String bindAddress,
    @DefaultValue("8185") int port, String advertiseUrl, String businessUrl, String registrationUrl,
    @DefaultValue("AIOPS_SDK_REGISTRATION_TOKEN") String tokenEnv,
    @DefaultValue("AIOPS_SDK_DIAGNOSTICS_TOKEN") String diagnosticsTokenEnv,
    @DefaultValue(".run/java-sdk") String stateDir,
    @DefaultValue("1024") int bufferCapacity,
    @DefaultValue("10") int successSampleEvery,
    @DefaultValue("true") boolean jdbcEnabled,
    @DefaultValue("true") boolean workersEnabled,
    @DefaultValue("true") boolean logsEnabled,
    Map<String, ProbePolicy> routes) {
    public SdkProperties {
        if (port<0 || port>65535 || bufferCapacity<32 || bufferCapacity>8192 || successSampleEvery<1 || successSampleEvery>10000)
            throw new IllegalArgumentException("Invalid bounded SDK configuration");
        routes=routes==null ? Map.of() : Map.copyOf(routes);
    }
    public record ProbePolicy(@DefaultValue("unknown") String effect,
        @DefaultValue("false") boolean probe, @DefaultValue("false") boolean load,
        @DefaultValue("20") int maxRequests, @DefaultValue("4") int maxConcurrency,
        @DefaultValue("10") int maxDurationSec, @DefaultValue("5") int maxRps) {
        public ProbePolicy {
            if (!java.util.Set.of("unknown","read_only","write").contains(effect) ||
                maxRequests<1 || maxRequests>200 || maxConcurrency<1 || maxConcurrency>16 ||
                maxDurationSec<1 || maxDurationSec>30 || maxRps<1 || maxRps>50 ||
                effect.equals("unknown") && (probe || load) || load && !probe)
                throw new IllegalArgumentException("Invalid endpoint probe policy");
        }
        public Map<String,Object> wire() {
            return Wire.map("effect",effect,"probe",probe,"load",load,"max_requests",maxRequests,
                "max_concurrency",maxConcurrency,"max_duration_sec",maxDurationSec,"max_rps",maxRps);
        }
    }
}
