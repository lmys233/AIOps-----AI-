package io.aiops.sdk;

import java.lang.reflect.Modifier;
import java.util.*;
import java.util.concurrent.*;
import org.aopalliance.intercept.MethodInterceptor;
import org.springframework.aop.framework.ProxyFactory;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

final class WorkerObservation {
    private WorkerObservation(){}
    static Object wrap(Object target,String name,SdkRuntime runtime){
        ThreadPoolExecutor pool=target instanceof ThreadPoolTaskExecutor spring?spring.getThreadPoolExecutor():(ThreadPoolExecutor)target;
        if(Modifier.isFinal(target.getClass().getModifiers()) || runtime.workers.size()>=64){
            runtime.capability("sdk_worker","partial","Unsupported/final executor or pool count limit","spring-executor");return target;
        }
        String id="worker-"+Wire.sha(name).substring(0,20);runtime.workers.put(id,pool);
        ProxyFactory proxy=new ProxyFactory(target);proxy.setProxyTargetClass(true);
        proxy.addAdvice((MethodInterceptor)invocation->{
            if(!Set.of("execute","submit").contains(invocation.getMethod().getName()) || invocation.getArguments().length==0)return invocation.proceed();
            Object task=invocation.getArguments()[0];if(!(task instanceof Runnable) && !(task instanceof Callable<?>))return invocation.proceed();
            String taskId=Wire.id();var context=RequestContext.current();context.put("worker_pool_id",id);context.put("task_id",taskId);
            long submitted=System.nanoTime();var metadata=Wire.map("pool_id",id,"capacity",pool.getMaximumPoolSize(),
                "queue_capacity",pool.getQueue().size()+pool.getQueue().remainingCapacity(),"active",pool.getActiveCount(),"queued",pool.getQueue().size()+1);
            runtime.emit("worker_submit","sdk_worker",metadata,context,false);
            if(task instanceof Runnable runnable)invocation.getArguments()[0]=(Runnable)()->{
                try{observed(()->{runnable.run();return null;},runtime,context,submitted,id);}
                catch(RuntimeException|Error e){throw e;}catch(Exception impossible){throw new IllegalStateException(impossible);}
            };
            else invocation.getArguments()[0]=(Callable<Object>)()->observed((Callable<?>)task,runtime,context,submitted,id);
            try {return invocation.proceed();}
            catch(RejectedExecutionException e){
                metadata.put("reason",pool.isShutdown()?"shutdown":pool.getActiveCount()>=pool.getMaximumPoolSize() && pool.getQueue().remainingCapacity()==0?"capacity":"unknown");
                metadata.putAll(JdbcObservation.exception(e));runtime.emit("worker_reject","sdk_worker",metadata,context,true);throw e;
            }
        });
        runtime.capability("sdk_worker","available","Spring-managed standard execute/submit; custom rejection handlers remain application-defined","spring-executor");
        return proxy.getProxy();
    }
    private static Object observed(Callable<?> task,SdkRuntime runtime,Map<String,String> context,long submitted,String id)throws Exception{
        long started=System.nanoTime();String outcome="success";
        try(RequestContext.Scope ignored=RequestContext.bind(context)){
            runtime.emit("worker_start","sdk_worker",Wire.map("pool_id",id,"wait_ms",(started-submitted)/1e6),context,false);
            try{return task.call();}catch(Exception|Error e){outcome="error";throw e;}
            finally {runtime.emit("worker_finish","sdk_worker",Wire.map("pool_id",id,"run_ms",(System.nanoTime()-started)/1e6,"outcome",outcome),context,false);}
        }
    }
}
