package io.aiops.sdk;

import java.lang.management.ManagementFactory;
import java.util.*;

final class JvmSnapshot {
    private JvmSnapshot(){}
    static Map<String,Object> capture(SdkRuntime runtime){
        long tick=System.nanoTime();var mx=ManagementFactory.getThreadMXBean();long[] all=mx.getAllThreadIds();
        long[] ids=Arrays.copyOf(all,Math.min(32,all.length));var infos=mx.getThreadInfo(ids,12);
        List<Map<String,Object>> stacks=new ArrayList<>();
        for(var info:infos){if(info==null || System.nanoTime()-tick>20_000_000)continue;
            stacks.add(Wire.map("thread_id",String.valueOf(info.getThreadId()),"thread_name",info.getThreadName(),"state",info.getThreadState().name(),
                "text",Arrays.stream(info.getStackTrace()).map(StackTraceElement::toString).reduce("",(a,b)->a+b+"\n"),
                "lock_name",info.getLockName(),"lock_owner_id",info.getLockOwnerId()<0?null:String.valueOf(info.getLockOwnerId())));
        }
        List<Map<String,Object>> workers=new ArrayList<>();runtime.workers.forEach((id,pool)->workers.add(Wire.map(
            "pool_id",id,"capacity",pool.getMaximumPoolSize(),"queue_capacity",pool.getQueue().size()+pool.getQueue().remainingCapacity(),
            "active",pool.getActiveCount(),"queued",pool.getQueue().size(),"shutdown",pool.isShutdown(),"tasks",List.of())));
        var payload=Wire.map("stacks",stacks,"async_tasks",List.of(),"workers",workers,"holders",new ArrayList<>(runtime.holders.values()),
            "capture_ms",(System.nanoTime()-tick)/1e6,"sampling_interval_seconds",0,
            "note","Bounded point-in-time JVM view; absent stacks do not exclude historical waits");
        return runtime.emit("runtime_snapshot","sdk_runtime",payload,RequestContext.current(),true);
    }
}
