package io.aiops.sdk;

import com.sun.net.httpserver.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;

/** Separate SDK listener avoids taking over the application's /health or /metrics routes. */
final class SdkHttpServer implements AutoCloseable {
    private final SdkRuntime runtime;private HttpServer server;private ExecutorService executor;
    private final AtomicLong lastSnapshot=new AtomicLong();
    SdkHttpServer(SdkRuntime runtime){this.runtime=runtime;}
    void start()throws IOException{
        server=HttpServer.create(new InetSocketAddress(runtime.config.bindAddress(),runtime.config.port()),64);
        executor=new ThreadPoolExecutor(2,4,30,TimeUnit.SECONDS,new ArrayBlockingQueue<>(64),r->{var thread=new Thread(r,"aiops-sdk-http");thread.setDaemon(true);return thread;},new ThreadPoolExecutor.AbortPolicy());
        server.setExecutor(executor);server.createContext("/",this::handle);server.start();
        runtime.sdkOrigin=Wire.origin(SdkRuntime.first(runtime.config.advertiseUrl(),"http://127.0.0.1:"+server.getAddress().getPort()));
    }
    private void handle(HttpExchange exchange)throws IOException{
        try {
            String path=exchange.getRequestURI().getPath();
            if(path.startsWith("/_aiops/")){
                String token=SdkRuntime.first(runtime.readToken,System.getenv(runtime.config.diagnosticsTokenEnv()));
                if(token.isEmpty()){send(exchange,404,Wire.map("error","diagnostic_not_configured"));return;}
                if(!MessageDigest.isEqual(("Bearer "+token).getBytes(StandardCharsets.UTF_8),String.valueOf(exchange.getRequestHeaders().getFirst("Authorization")).getBytes(StandardCharsets.UTF_8))){send(exchange,401,Wire.map("error","unauthorized"));return;}
            }
            if(!exchange.getRequestMethod().equals("GET")){send(exchange,405,Wire.map("error","read_only"));return;}
            switch(path){
                case "/health" -> {var value=runtime.identity();value.putAll(Wire.map("status","alive","sdk_version",Wire.VERSION,"contract_version","spec-2026.01","downstream",Map.of(),"registration_status",runtime.registrationStatus));send(exchange,200,value);}
                case "/ready" -> send(exchange,200,Wire.map("status","ready","service",runtime.service(),"checks",List.of(Wire.map("kind","application","ok",true,"detail","Spring application started; this endpoint does not run dependency probes"))));
                case "/metrics" -> {byte[] text=runtime.metrics.scrape().getBytes(StandardCharsets.UTF_8);exchange.getResponseHeaders().set("Content-Type","text/plain; version=0.0.4; charset=utf-8");exchange.sendResponseHeaders(200,text.length);exchange.getResponseBody().write(text);}
                case "/services" -> send(exchange,200,Wire.map("status","ok","services",List.of(Wire.map("name",runtime.service(),"base_url",runtime.businessOrigin,"identity",runtime.identity(),"downstream",Map.of()))));
                case "/changes" -> send(exchange,200,Wire.map("identity",runtime.identity(),"events",runtime.events().stream().filter(r->r.get("kind").equals("config_change")).toList(),"complete",false));
                case "/_aiops/capabilities" -> send(exchange,200,runtime.capabilities());
                case "/_aiops/snapshot" -> {
                    long now=System.nanoTime(),last=lastSnapshot.get();
                    if(last!=0 && now-last<2_000_000_000L || !lastSnapshot.compareAndSet(last,now)){
                        exchange.getResponseHeaders().set("Retry-After","2");send(exchange,429,Wire.map("error","snapshot_rate_limit","retry_after_sec",2));
                    } else {var event=JvmSnapshot.capture(runtime);send(exchange,event==null?503:200,event==null?Wire.map("error","snapshot_unavailable"):event);}
                }
                case "/_aiops/probe-observations" -> {
                    String query=exchange.getRequestURI().getRawQuery(),run=null;
                    if(query!=null && query.length()<1024)for(String pair:query.split("&"))if(pair.startsWith("run_id="))run=URLDecoder.decode(pair.substring(7),StandardCharsets.UTF_8);
                    if(run==null || !run.matches("probe_[a-f0-9]{32}")){send(exchange,422,Wire.map("error","invalid_probe_id"));return;}
                    String selected=run;
                    List<Map<String,Object>> rows=runtime.events().stream().filter(r->selected.equals(((Map<?,?>)r.get("correlation")).get("probe_id"))).toList();
                    List<Map<String,Object>> events=new ArrayList<>();int bytes=0;
                    for(int n=rows.size()-1;n>=0;n--){var row=rows.get(n);int length=Wire.json(row).getBytes(StandardCharsets.UTF_8).length;
                        if(events.size()>=100 || bytes+length>500000)break;events.add(row);bytes+=length;}
                    send(exchange,200,Wire.map("identity",runtime.identity(),"events",events,"truncated",events.size()<rows.size(),"complete",false,"note","Bounded local observations; cannot prove complete historical coverage"));
                }
                default -> send(exchange,404,Wire.map("error","not_found"));
            }
        } catch(Exception ignored){runtime.dropped.incrementAndGet();if(exchange.getResponseCode()==-1)send(exchange,503,Wire.map("error","sdk_read_failed"));}
        finally {exchange.close();}
    }
    private static void send(HttpExchange exchange,int status,Object data)throws IOException{
        byte[] body=Wire.json(data).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type","application/json; charset=utf-8");exchange.getResponseHeaders().set("Cache-Control","no-store");
        exchange.sendResponseHeaders(status,body.length);exchange.getResponseBody().write(body);
    }
    @Override public void close(){if(server!=null)server.stop(0);if(executor!=null)executor.shutdownNow();}
}
