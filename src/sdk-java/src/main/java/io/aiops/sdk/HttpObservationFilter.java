package io.aiops.sdk;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.IOException;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.slf4j.MDC;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.servlet.HandlerMapping;

final class HttpObservationFilter extends OncePerRequestFilter {
    private final SdkRuntime runtime;
    HttpObservationFilter(SdkRuntime runtime){this.runtime=runtime;}
    @Override protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain) throws ServletException,IOException{
        Map<String,String> context=RequestContext.inbound(request.getHeader("traceparent"),request.getHeader("X-AIOps-Probe-Id"),runtime.readToken,
            request.getMethod(),request.getRequestURI(),request.getHeader("X-AIOps-Request-Id"),request.getHeader("X-AIOps-Probe-Proof"));
        String startedAt=Wire.now();long started=System.nanoTime(),epoch=RequestContext.epochNanos();
        var recorded=new AtomicBoolean();Throwable[] failure={null};Map<String,String> priorMdc=MDC.getCopyOfContextMap();
        try(RequestContext.Scope ignored=RequestContext.bind(context)){
            MDC.put("trace_id",context.get("trace_id"));MDC.put("span_id",context.get("span_id"));MDC.put("request_id",context.get("request_id"));
            try {chain.doFilter(request,response);}
            catch(IOException|ServletException|RuntimeException|Error e){failure[0]=e;throw e;}
            finally {
                if(request.isAsyncStarted()){
                    AsyncListener observer=new AsyncListener(){
                        public void onComplete(AsyncEvent event){record(request,response,startedAt,started,epoch,context,failure[0],recorded,false);}
                        public void onTimeout(AsyncEvent event){record(request,response,startedAt,started,epoch,context,event.getThrowable(),recorded,true);}
                        public void onError(AsyncEvent event){failure[0]=event.getThrowable();}
                        public void onStartAsync(AsyncEvent event){event.getAsyncContext().addListener(this);}
                    };
                    try {request.getAsyncContext().addListener(observer);}
                    catch(IllegalStateException race){record(request,response,startedAt,started,epoch,context,failure[0],recorded,false);}
                } else record(request,response,startedAt,started,epoch,context,failure[0],recorded,false);
            }
        } finally {if(priorMdc==null)MDC.clear();else MDC.setContextMap(priorMdc);}
    }
    private void record(HttpServletRequest request,HttpServletResponse response,String startedAt,long started,long epoch,Map<String,String> context,
        Throwable failure,AtomicBoolean recorded,boolean timeout){
        if(!recorded.compareAndSet(false,true))return;
        try {
            Object pattern=request.getAttribute(HandlerMapping.BEST_MATCHING_PATTERN_ATTRIBUTE);
            String route=pattern==null?"<unmatched>":String.valueOf(pattern);
            String outcome=timeout?"timeout":failure!=null?"exception":response.getStatus()>=500?"server_error":"complete";
            Integer status=failure!=null && response.getStatus()<400?null:response.getStatus();
            var payload=Wire.map("route",route,"method",request.getMethod(),"outcome",outcome,"status",status,
                "total_ms",(System.nanoTime()-started)/1e6,"headers_ms",null,"completed",!timeout && failure==null,
                "request_started_at",startedAt);
            if(failure!=null)payload.putAll(JdbcObservation.exception(failure));
            runtime.emit("http_request","sdk_http",payload,context,false);
            runtime.metrics.http(request.getMethod(),route,status==null?"exception":String.valueOf(status),((Number)payload.get("total_ms")).doubleValue()/1000);
            RequestContext.span(runtime,request.getMethod()+" "+route,"SERVER",epoch,RequestContext.epochNanos(),
                failure!=null || timeout || response.getStatus()>=500?"ERROR":"OK",context);
        } catch(Exception ignored){runtime.dropped.incrementAndGet();}
    }
}
