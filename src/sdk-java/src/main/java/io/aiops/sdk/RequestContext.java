package io.aiops.sdk;

import java.util.*;

/** Only bounded correlation identifiers travel to child tasks or downstream calls. */
public final class RequestContext {
    private static final ThreadLocal<Map<String,String>> LOCAL=ThreadLocal.withInitial(Map::of);
    private RequestContext(){}
    public static Map<String,String> current(){return new LinkedHashMap<>(LOCAL.get());}
    public static Scope bind(Map<String,String> value){var old=LOCAL.get();LOCAL.set(Map.copyOf(value));return ()->LOCAL.set(old);}
    public static Map<String,String> inbound(String parent,String probe,String enrollment,String method,String path,String nonce,String signature){
        Map<String,String> context=new LinkedHashMap<>();context.put("request_id",Wire.id());
        if(parent!=null && parent.matches("00-[a-f0-9]{32}-[a-f0-9]{16}-[a-f0-9]{2}") &&
            !parent.substring(3,35).equals("0".repeat(32)) && !parent.substring(36,52).equals("0".repeat(16))) {
            context.put("trace_id",parent.substring(3,35));context.put("parent_span_id",parent.substring(36,52));
        } else context.put("trace_id",Wire.id());
        context.put("span_id",Wire.id().substring(0,16));
        // Active-probe headers are accepted only after exact platform HMAC verification.
        if(ProbeCorrelation.valid(enrollment,probe,method,path,nonce,signature)) {
            context.put("probe_id",probe);context.put("traffic_origin","active_probe");
            context.put("request_id",nonce);
        }
        return context;
    }
    public static void span(SdkRuntime runtime,String name,String kind,long started,long ended,String status,Map<String,String> context){
        runtime.emit("trace_span","sdk_trace",Wire.map("name",Wire.safe(name,512),"kind",kind,"span_id",context.get("span_id"),
            "parent_span_id",context.get("parent_span_id"),"start_ns",started,"end_ns",ended,"status",status,
            "attributes",Map.of()),context,false);
    }
    public static long epochNanos(){var now=java.time.Instant.now();return now.getEpochSecond()*1_000_000_000L+now.getNano();}
    public interface Scope extends AutoCloseable { @Override void close(); }
}
