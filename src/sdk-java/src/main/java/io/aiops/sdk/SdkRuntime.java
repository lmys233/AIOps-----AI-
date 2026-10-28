package io.aiops.sdk;

import java.lang.management.ManagementFactory;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.core.env.Environment;

/** Bounded side channel. emit() never does network/disk I/O or throws into business code. */
public final class SdkRuntime {
    final SdkProperties config;
    final Map<String,Object> capabilities=new ConcurrentHashMap<>();
    final Map<String,Map<String,Object>> holders=new ConcurrentHashMap<>();
    final Map<String,java.util.concurrent.ThreadPoolExecutor> workers=new ConcurrentHashMap<>();
    final Map<String,javax.sql.DataSource> pools=new ConcurrentHashMap<>();
    private final Deque<Map<String,Object>> events=new ArrayDeque<>();
    private final Map<String,AtomicLong> samples=new ConcurrentHashMap<>();
    private volatile Map<String,Object> identity;
    volatile List<Map<String,Object>> endpoints=List.of();
    volatile String endpointStatus="unsupported",readToken="",registrationStatus="unconfigured";
    volatile String sdkOrigin="",businessOrigin="";
    volatile int businessPort;
    final AtomicLong dropped=new AtomicLong(),sampled=new AtomicLong();
    final SdkMetrics metrics=new SdkMetrics(this);

    public SdkRuntime(SdkProperties config,Environment environment){
        this.config=config;
        String service=first(config.serviceName(),environment.getProperty("spring.application.name"),"application");
        String instance=first(config.instance(),System.getenv("AIOPS_SDK_INSTANCE"),"java-"+ManagementFactory.getRuntimeMXBean().getName().replace('@',':'));
        identity=Wire.map("tenant","default","env",Wire.identifier(config.env()),"cluster",empty(config.cluster()),
            "service",Wire.identifier(service),"instance",Wire.identifier(instance),"boot_id",Wire.id());
        capability("sdk_http","available","Servlet synchronous and async completion","spring-mvc");
        capability("sdk_trace","available","W3C server/client spans exported as scoped diagnostic events","w3c-event-spans");
        capability("sdk_sql","unsupported","No supported DataSource mounted","jdbc-proxy");
        capability("sdk_pool","unsupported","No supported connection pool mounted","jdbc-proxy");
        capability("sdk_worker","unsupported","No Spring-managed supported executor mounted","spring-executor");
        capability("request_cancel","partial","Java interruption does not identify a cancellation initiator","servlet");
        capability("request_disconnect","partial","IOException alone cannot prove client disconnect","servlet");
        capability("db_identity","unconfigured","No extra database identity queries by default","jdbc");
        capability("db_ddl","unsupported","Requires independent database audit source","");
        capability("process_exit","unsupported","Requires external process/container observer","");
        capability("registration","unconfigured","Enrollment token and registration URL required","");
        capability("diagnostic_export","unconfigured","Registration not completed","");
        capability("runtime_stacks","available","Point-in-time bounded JVM thread snapshot","thread-mxbean");
    }
    static String first(String... values){for(String value:values)if(value!=null && !value.isBlank())return value;return "";}
    static String empty(String value){return value==null || value.isBlank() ? null : Wire.identifier(value);}
    public Map<String,Object> identity(){return new LinkedHashMap<>(identity);}
    public String service(){return (String)identity.get("service");}
    public boolean registered(){return identity.containsKey("observed_id");}
    synchronized void bind(String tenant,String observedId,String token){
        if(!observedId.matches("obs_[a-f0-9]{32}"))throw new IllegalArgumentException("Invalid registered system ID");
        var updated=identity();updated.put("tenant",Wire.identifier(tenant));updated.put("observed_id",observedId);
        for(var row:events){
            @SuppressWarnings("unchecked") var old=(Map<String,Object>)row.get("identity");
            if(!old.containsKey("observed_id") && Objects.equals(old.get("boot_id"),updated.get("boot_id"))){
                row.put("identity",new LinkedHashMap<>(updated));row.put("schema_version",2);
            }
        }
        identity=Collections.unmodifiableMap(new LinkedHashMap<>(updated));
        readToken=Wire.pullToken(token,identity);registrationStatus="registered_unverified";
        capability("registration","available","Registered; no heartbeat or current business health assertion","http-enrollment");
    }
    public void capability(String name,String status,String reason,String adapter){
        capabilities.put(name,Wire.map("status",status,"reason",reason,"adapter",adapter,"mounted",status.equals("available"),"last_success",null));
    }
    public Map<String,Object> emit(String kind,String source,Map<String,Object> payload){return emit(kind,source,payload,RequestContext.current(),false);}
    public Map<String,Object> emit(String kind,String source,Map<String,Object> payload,Map<String,String> correlation,boolean force){
        try {
            boolean important=important(kind,payload);
            boolean limited=!important && !force && !correlation.containsKey("probe_id") && config.successSampleEvery()>1;
            if(limited && (samples.computeIfAbsent(kind,k->new AtomicLong()).getAndIncrement()%config.successSampleEvery()!=0)){
                sampled.incrementAndGet();return null;
            }
            String at=Wire.now();Map<String,Object> clean=cleanMap(payload,0);
            boolean truncated=!Wire.json(clean).equals(Wire.json(payload));
            if(Wire.json(clean).getBytes(java.nio.charset.StandardCharsets.UTF_8).length>48000){dropped.incrementAndGet();return null;}
            String start=String.valueOf(payload.getOrDefault("request_started_at",payload.getOrDefault("execution_started_at",at)));
            boolean complete=!limited && !truncated && !kind.equals("runtime_snapshot");
            var row=Wire.map("schema_version",registered()?2:1,"event_id",Wire.id(),"observation_id",Wire.id(),"identity",identity(),
                "kind",kind,"source",source,"occurred_at",at,"captured_at",at,
                "coverage",Wire.map("start",start,"end",at,"complete",complete),
                "quality",Wire.map("status",limited||truncated ? "partial":"available","reason",limited?"Successful observations sampled":truncated?"Payload redacted or bounded":"",
                    "sampled",limited,"truncated",truncated,"dropped",0),
                "correlation",new LinkedHashMap<>(correlation),"payload",clean,"raw_ref","");
            synchronized(this){
                if(events.size()>=config.bufferCapacity()){
                    var removable=events.stream().filter(r->!important((String)r.get("kind"),castPayload(r))).findFirst().orElse(null);
                    if(removable!=null)events.remove(removable);else if(!important){dropped.incrementAndGet();return null;}else events.removeFirst();
                    dropped.incrementAndGet();
                }
                events.addLast(row);
            }
            return row;
        } catch(Exception ignored){dropped.incrementAndGet();return null;}
    }
    @SuppressWarnings("unchecked") static Map<String,Object> castPayload(Map<String,Object> row){return (Map<String,Object>)row.get("payload");}
    static boolean important(String kind,Map<String,Object> payload){return kind.contains("error") || kind.contains("timeout") || kind.contains("reject") ||
        Set.of("error","exception","server_error","timeout","cancelled").contains(String.valueOf(payload.get("outcome"))) ||
        Set.of("ERROR","FATAL","CRITICAL").contains(String.valueOf(payload.get("level"))) ||
        kind.equals("trace_span") && "ERROR".equals(payload.get("status")) ||
        payload.get("total_ms") instanceof Number total && total.doubleValue()>=1000 ||
        payload.get("execution_ms") instanceof Number execution && execution.doubleValue()>=500;}
    private static Map<String,Object> cleanMap(Map<String,Object> value,int depth){
        var result=new LinkedHashMap<String,Object>();int n=0;
        for(var entry:value.entrySet()){
            if(n++>=64)break;
            result.put(entry.getKey(),entry.getKey().matches("(?i).*?(password|secret|token|authorization|credential).*?") ? "***" : clean(entry.getValue(),depth+1));
        }
        return result;
    }
    @SuppressWarnings("unchecked") private static Object clean(Object value,int depth){
        if(depth>12)return "<depth-limit>";
        if(value instanceof String s)return Wire.safe(s,8000);
        if(value instanceof Map<?,?> m)return cleanMap((Map<String,Object>)m,depth);
        if(value instanceof Collection<?> c)return c.stream().limit(256).map(v->clean(v,depth+1)).toList();
        return value;
    }
    public synchronized List<Map<String,Object>> events(){return events.stream().<Map<String,Object>>map(r->new LinkedHashMap<>(r)).toList();}
    public Map<String,Object> capabilities(){
        return Wire.map("identity",identity(),"capabilities",new TreeMap<>(capabilities),"buffered",events().size(),"dropped",dropped.get(),
            "captured_at",Wire.now(),"enabled",true,"sampled_successes",sampled.get(),"sdk_version",Wire.VERSION,
            "schema_version",registered()?2:1,"limits",Wire.map("events",config.bufferCapacity(),"worker_pools",64,"tracked_tasks_per_pool",1024,
                "routes",256,"snapshot_threads",32,"snapshot_depth",12,"snapshot_bytes",48000));
    }
}
