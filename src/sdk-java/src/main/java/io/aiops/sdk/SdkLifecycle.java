package io.aiops.sdk;

import java.net.URI;
import java.io.IOException;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.boot.web.context.WebServerInitializedEvent;
import org.springframework.context.ApplicationListener;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/** Registration is finite; export is asynchronous. No platform heartbeat polls the business. */
final class SdkLifecycle implements ApplicationListener<ApplicationReadyEvent>,AutoCloseable {
    private final SdkRuntime runtime;private final RequestMappingHandlerMapping mapping;
    private final ScheduledExecutorService worker=Executors.newSingleThreadScheduledExecutor(r->{var thread=new Thread(r,"aiops-sdk-export");thread.setDaemon(true);return thread;});
    private final HttpClient client=HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).connectTimeout(Duration.ofSeconds(2)).followRedirects(HttpClient.Redirect.NEVER).build();
    private final AtomicBoolean started=new AtomicBoolean();private final Set<String> seen=new LinkedHashSet<>();
    private final SdkHttpServer server;private int attempts,exportFailures;private boolean registrationBlocked,exportBlocked;
    private long nextRegistration,lastReport,nextExport;private String enrollment="",registrationUrl="";private Path state;private JsonOutbox outbox;
    SdkLifecycle(SdkRuntime runtime,RequestMappingHandlerMapping mapping){this.runtime=runtime;this.mapping=mapping;server=new SdkHttpServer(runtime);}
    void webServer(WebServerInitializedEvent event){if(event.getApplicationContext().getParent()==null)runtime.businessPort=event.getWebServer().getPort();}
    @Override public void onApplicationEvent(ApplicationReadyEvent event){
        if(!started.compareAndSet(false,true))return;
        try {
            runtime.endpoints=EndpointDiscovery.discover(mapping,runtime.config);runtime.endpointStatus="complete";
        } catch(Exception e){runtime.endpointStatus="unavailable:"+e.getClass().getSimpleName();runtime.endpoints=List.of();}
        try {
            runtime.businessOrigin=Wire.origin(SdkRuntime.first(runtime.config.businessUrl(),"http://127.0.0.1:"+runtime.businessPort));server.start();
            enrollment=SdkRuntime.first(System.getenv(runtime.config.tokenEnv()));registrationUrl=SdkRuntime.first(runtime.config.registrationUrl());
            String key=Wire.sha(String.join("|",SdkRuntime.first(runtime.config.observedName()),runtime.service(),String.valueOf(runtime.identity().get("instance")),
                String.valueOf(runtime.identity().get("env")),String.valueOf(runtime.identity().get("cluster")),Wire.sha(enrollment))).substring(0,32);
            state=Path.of(runtime.config.stateDir()).resolve(key);
            worker.scheduleWithFixedDelay(this::tick,0,1,TimeUnit.SECONDS);
            runtime.emit("process_start","sdk_lifecycle",Wire.map("pid",ProcessHandle.current().pid(),"runtime","java","sdk_version",Wire.VERSION),Map.of(),true);
        } catch(Exception e){runtime.capability("registration","query_failed","SDK listener/setup failed: "+e.getClass().getSimpleName(),"spring-lifecycle");}
    }
    private void tick(){
        try {
            if(!runtime.registered()){
                if(registrationUrl.isBlank() || enrollment.isBlank() || SdkRuntime.first(runtime.config.observedName()).isEmpty())return;
                if(attempts>=8 || registrationBlocked || System.currentTimeMillis()<nextRegistration)return;
                try {register();}catch(Exception error){
                    org.slf4j.LoggerFactory.getLogger(SdkLifecycle.class).warn("SDK registration attempt {} failed: {}",attempts,Wire.safe(error.toString(),500));
                    runtime.registrationStatus="query_failed";runtime.capability("registration","query_failed",error.getClass().getSimpleName(),"http-enrollment");
                    if(error instanceof SecurityException || error instanceof IllegalArgumentException)registrationBlocked=true;
                    nextRegistration=System.currentTimeMillis()+Math.min(10000,200L*(1L<<Math.min(attempts,6)));
                }
                if(!runtime.registered())return;
            }
            if(outbox==null)outbox=new JsonOutbox(state.resolve("outbox.json"),4096);
            if(System.currentTimeMillis()-lastReport>30000){
                runtime.emit("capability_report","sdk_runtime",runtime.capabilities(),Map.of(),true);lastReport=System.currentTimeMillis();
            }
            List<Map<String,Object>> fresh=runtime.events().stream().filter(e->!seen.contains(e.get("event_id"))).toList();
            if(!fresh.isEmpty()){long before=outbox.dropped;outbox.add(fresh);runtime.dropped.addAndGet(outbox.dropped-before);
                fresh.forEach(e->seen.add((String)e.get("event_id")));while(seen.size()>8192)seen.remove(seen.iterator().next());}
            if(exportBlocked || System.currentTimeMillis()<nextExport)return;
            List<Map<String,Object>> batch=outbox.batch();if(batch.isEmpty())return;
            Map<String,Object> response=post(registrationUrl.replace("/v1/observed/register","/v1/observations/batches"),Wire.map("schema_version",2,"events",batch));
            List<String> ids=batch.stream().map(e->(String)e.get("event_id")).toList();
            if(!ids.equals(response.get("accepted_ids")))throw new IllegalStateException("Exact event acknowledgment missing");
            outbox.ack(ids);exportFailures=0;runtime.capability("diagnostic_export","available","Batch acknowledged and persisted by receiver","http-batches");
        } catch(Exception error){
            if(error instanceof SecurityException || error instanceof IllegalArgumentException)exportBlocked=true;
            nextExport=System.currentTimeMillis()+Math.min(10000,500L*(1L<<Math.min(++exportFailures,4)));
            runtime.capability("diagnostic_export",error instanceof SecurityException?"permission_denied":"query_failed",error.getClass().getSimpleName()+"; bounded outbox retained","http-batches");
        }
    }
    List<Map<String,Object>> manifestCapabilities(){return Wire.CAPABILITIES.entrySet().stream().sorted(Map.Entry.comparingByKey()).map(e->Wire.map("id",e.getKey(),"method","GET","path",e.getValue(),"version",1,"enabled",true)).toList();}
    Map<String,Object> manifest()throws Exception{
        Map<String,Object> identity=runtime.identity();String observedId=null;Path saved=state.resolve("registration.json");
        if(Files.exists(saved)){
            if(Files.size(saved)>16384)throw new IllegalArgumentException("Registration cache too large");
            observedId=(String)Wire.JSON.readValue(Files.readString(saved),Map.class).get("observed_id");
            if(observedId==null || !observedId.matches("obs_[a-f0-9]{32}"))throw new IllegalArgumentException("Invalid cached observed ID");
        }
        return Wire.map("schema_version",2,"observed_name",runtime.config.observedName(),"observed_id",observedId,"service",identity.get("service"),
            "instance",identity.get("instance"),"boot_id",identity.get("boot_id"),"env",identity.get("env"),"cluster",identity.get("cluster"),
            "base_url",runtime.sdkOrigin,"business_url",runtime.businessOrigin,"sdk_version",Wire.VERSION,"capabilities",manifestCapabilities(),
            "endpoints",runtime.endpoints,"endpoint_status",runtime.endpointStatus,"endpoint_revision",Wire.sha(Wire.canonical(runtime.endpoints)));
    }
    private void register()throws Exception{
        attempts++;
        if(!registrationUrl.endsWith("/v1/observed/register"))throw new IllegalArgumentException("Invalid fixed registration path");
        Wire.origin(registrationUrl.substring(0,registrationUrl.length()-"/v1/observed/register".length()));
        Map<String,Object> manifest=manifest();
        Files.createDirectories(state);Files.writeString(state.resolve("manifest.json"),Wire.json(manifest));
        Map<String,Object> response=post(registrationUrl,manifest);
        for(String key:List.of("service","instance","boot_id"))if(!Objects.equals(response.get(key),manifest.get(key)))throw new IllegalArgumentException("Registration identity mismatch");
        String oid=(String)response.get("observed_id");if(manifest.get("observed_id")!=null && !manifest.get("observed_id").equals(oid))throw new IllegalArgumentException("Registration system changed");
        runtime.bind((String)response.get("tenant"),oid,enrollment);
        Files.createDirectories(state);Path temp=state.resolve("registration.tmp");Files.writeString(temp,Wire.json(Wire.map("observed_id",oid)));
        Files.move(temp,state.resolve("registration.json"),StandardCopyOption.REPLACE_EXISTING);
        runtime.emit("service_manifest","sdk_runtime",Wire.map("downstream",Map.of(),"runtime","java"),Map.of(),true);
        configurationChanged();
    }
    @SuppressWarnings("unchecked") private void configurationChanged()throws Exception{
        Map<String,Object> configuration=Wire.map("sdk.version",Wire.VERSION,"sdk.env",runtime.identity().get("env"),"sdk.cluster",runtime.identity().get("cluster"),
            "sdk.service",runtime.service(),"sdk.jdbc_enabled",runtime.config.jdbcEnabled(),"sdk.workers_enabled",runtime.config.workersEnabled(),
            "sdk.success_sample_every",runtime.config.successSampleEvery(),"sdk.endpoint_revision",Wire.sha(Wire.canonical(runtime.endpoints)));
        Path path=state.resolve("config.json");Map<String,Object> old=Map.of();
        if(Files.exists(path) && Files.size(path)<=16384)old=Wire.JSON.readValue(Files.readString(path),Map.class);
        List<Map<String,Object>> diff=new ArrayList<>();
        for(var entry:configuration.entrySet())if(!Objects.equals(old.get(entry.getKey()),entry.getValue()))
            diff.add(Wire.map("field",entry.getKey(),"old_value",old.get(entry.getKey()),"new_value",entry.getValue(),"action_hash",null,"scenario_id",null));
        if(!diff.isEmpty())runtime.emit("config_change","sdk_runtime",Wire.map("diff",diff,"source","startup","entity_id","svc:"+runtime.service(),
            "config_fingerprint",Wire.sha(Wire.canonical(configuration))),Map.of(),true);
        Path temp=state.resolve("config.tmp");Files.writeString(temp,Wire.json(configuration));Files.move(temp,path,StandardCopyOption.REPLACE_EXISTING);
    }
    @SuppressWarnings("unchecked") private Map<String,Object> post(String url,Object body)throws Exception{
        var request=HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(2)).header("Authorization","Bearer "+enrollment)
            .header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString(Wire.json(body),StandardCharsets.UTF_8)).build();
        var response=client.send(request,info->new LimitedBodySubscriber(16384));
        {
            byte[] data=response.body();
            if(response.statusCode()==401 || response.statusCode()==403)throw new SecurityException("Enrollment permission denied");
            if(Set.of(400,404,422).contains(response.statusCode()))throw new IllegalArgumentException("SDK receiver rejected request: HTTP "+response.statusCode());
            if(response.statusCode()<200 || response.statusCode()>=300)throw new IOException("SDK receiver HTTP "+response.statusCode());
            return Wire.JSON.readValue(data,Map.class);
        }
    }
    @Override public void close(){
        worker.shutdownNow();server.close();
        try{worker.awaitTermination(2500,TimeUnit.MILLISECONDS);}catch(InterruptedException e){Thread.currentThread().interrupt();}
    }
}
