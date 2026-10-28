package io.aiops.sdk;

import java.io.IOException;
import java.util.*;
import org.springframework.http.HttpRequest;
import org.springframework.http.client.*;

final class DownstreamObservation implements ClientHttpRequestInterceptor {
    private final SdkRuntime runtime;
    DownstreamObservation(SdkRuntime runtime){this.runtime=runtime;}
    @Override public ClientHttpResponse intercept(HttpRequest request,byte[] body,ClientHttpRequestExecution execution)throws IOException{
        var context=RequestContext.current();
        if(!context.containsKey("trace_id"))context.put("trace_id",Wire.id());
        String parent=context.get("span_id");if(parent!=null)context.put("parent_span_id",parent);
        context.put("span_id",Wire.id().substring(0,16));
        if(!request.getHeaders().containsKey("traceparent"))request.getHeaders().set("traceparent","00-"+context.get("trace_id")+"-"+context.get("span_id")+"-01");
        String target=SdkRuntime.first(request.getURI().getHost(),"unknown"),method=request.getMethod().name();
        long started=System.nanoTime(),epoch=RequestContext.epochNanos();int status=0;Throwable failure=null;
        try{var response=execution.execute(request,body);status=response.getStatusCode().value();return response;}
        catch(IOException|RuntimeException e){failure=e;throw e;}
        finally {
            double elapsed=(System.nanoTime()-started)/1e6;
            var payload=Wire.map("target",target,"method",method,"status",status==0?null:status,"elapsed_ms",elapsed,
                "outcome",failure!=null?"error":status>=500?"server_error":"success","phase","response_headers",
                "note","Response body reading is owned by the caller; headers timing is not complete download duration");
            if(failure!=null)payload.putAll(JdbcObservation.exception(failure));
            runtime.emit("downstream_call","sdk_http",payload,context,false);runtime.metrics.downstream(target,method,status,elapsed/1000);
            RequestContext.span(runtime,method+" "+target,"CLIENT",epoch,RequestContext.epochNanos(),failure!=null || status>=500?"ERROR":"OK",context);
        }
    }
}
