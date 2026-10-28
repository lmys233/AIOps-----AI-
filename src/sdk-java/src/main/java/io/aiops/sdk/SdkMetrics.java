package io.aiops.sdk;

import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.DoubleSupplier;

/** Standard Prometheus text, bounded route labels and the existing Python metric names. */
final class SdkMetrics {
    private final SdkRuntime runtime;
    private final Map<String,Histogram> histograms=new ConcurrentHashMap<>();
    private final Map<String,DoubleSupplier> gauges=new ConcurrentHashMap<>();
    private static final double[] HTTP={.005,.01,.025,.05,.1,.25,.5,1,2.5,5,10};
    private static final double[] ACQUIRE={.001,.005,.01,.025,.05,.1,.25,.5,1,3,10};
    SdkMetrics(SdkRuntime runtime){this.runtime=runtime;}
    void http(String method,String route,String status,double seconds){observe("http_request_duration_seconds",Wire.map("method",method,"route",route,"status",status),seconds,HTTP);}
    void acquire(double seconds){observe("db_pool_acquire_seconds",Map.of(),seconds,ACQUIRE);}
    void downstream(String target,String method,int status,double seconds){
        observe("downstream_request_duration_seconds",Wire.map("target",target,"method",method,"status",String.valueOf(status)),seconds,HTTP);
        observe("downstream_requests",Wire.map("target",target,"status",String.valueOf(status)),0,new double[0]);
    }
    void gauge(String name,DoubleSupplier value){gauges.put(name,value);}
    private void observe(String name,Map<String,Object> tags,double value,double[] buckets){
        String key=name+Wire.canonical(tags);
        if(!histograms.containsKey(key) && histograms.size()>=1024)return;
        histograms.computeIfAbsent(key,k->new Histogram(name,tags,buckets)).record(value);
    }
    String scrape(){
        StringBuilder out=new StringBuilder();Map<String,Object> identity=runtime.identity();
        Map<String,Object> labels=Wire.map("service",runtime.service(),"service_instance_id",identity.get("instance"),"tenant",identity.get("tenant"),"env",identity.get("env"));
        if(runtime.registered())labels.put("observed_id",identity.get("observed_id"));
        if(identity.get("cluster")!=null)labels.put("cluster",identity.get("cluster"));
        histograms.values().stream().sorted(Comparator.comparing(h->h.name+Wire.canonical(h.tags))).forEach(h->h.write(out,labels));
        gauges.entrySet().stream().sorted(Map.Entry.comparingByKey()).forEach(e->{try{line(out,e.getKey(),labels,e.getValue().getAsDouble());}catch(Exception ignored){}});
        line(out,"aiops_sdk_dropped_events_total",labels,runtime.dropped.get());
        line(out,"jvm_memory_used_bytes",labels,Runtime.getRuntime().totalMemory()-Runtime.getRuntime().freeMemory());
        line(out,"jvm_threads_live_threads",labels,java.lang.management.ManagementFactory.getThreadMXBean().getThreadCount());
        return out.toString();
    }
    private static void line(StringBuilder out,String name,Map<String,Object> labels,double value){
        out.append(name).append('{');boolean first=true;
        for(var e:new TreeMap<>(labels).entrySet()){
            if(!first)out.append(',');first=false;out.append(e.getKey()).append("=\"").append(String.valueOf(e.getValue()).replace("\\","\\\\").replace("\"","\\\"").replace("\n","\\n")).append('"');
        }
        out.append("} ").append(value).append('\n');
    }
    private static final class Histogram {
        final String name;final Map<String,Object> tags;final double[] boundaries;final long[] counts;long count;double sum;
        Histogram(String name,Map<String,Object> tags,double[] boundaries){this.name=name;this.tags=tags;this.boundaries=boundaries;counts=new long[boundaries.length];}
        synchronized void record(double value){count++;sum+=value;for(int i=0;i<boundaries.length;i++)if(value<=boundaries[i])counts[i]++;}
        synchronized void write(StringBuilder out,Map<String,Object> identity){
            var labels=new LinkedHashMap<>(identity);labels.putAll(tags);
            if(name.equals("downstream_requests")){line(out,name+"_total",labels,count);return;}
            for(int i=0;i<boundaries.length;i++){labels.put("le",String.valueOf(boundaries[i]));line(out,name+"_bucket",labels,counts[i]);}
            labels.put("le","+Inf");line(out,name+"_bucket",labels,count);labels.remove("le");
            line(out,name+"_count",labels,count);line(out,name+"_sum",labels,sum);
        }
    }
}
