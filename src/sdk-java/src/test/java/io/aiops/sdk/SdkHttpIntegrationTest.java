package io.aiops.sdk;

import static org.assertj.core.api.Assertions.*;
import java.net.URI;
import java.net.http.*;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.SpringBootConfiguration;
import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT,classes=SdkHttpIntegrationTest.Application.class,
    properties={"spring.application.name=renamed-java-http","aiops.sdk.port=0","aiops.sdk.env=dev","aiops.sdk.success-sample-every=1",
        "spring.datasource.url=jdbc:h2:mem:java_http_unit;DB_CLOSE_DELAY=-1","spring.sql.init.mode=never"})
class SdkHttpIntegrationTest {
    @LocalServerPort int port;
    @Autowired SdkRuntime runtime;
    private final HttpClient client=HttpClient.newHttpClient();
    HttpResponse<String> get(String url,String token)throws Exception{
        var request=HttpRequest.newBuilder(URI.create(url));if(token!=null)request.header("Authorization","Bearer "+token);
        return client.send(request.GET().build(),HttpResponse.BodyHandlers.ofString());
    }
    @Test void businessResponseAndLowIntrusionDiscovery(){
        assertThat(runtime.endpoints).anyMatch(e->e.get("path").equals("/records/{id}"));
        assertThat(runtime.endpoints).allMatch(e->e.get("body_schema")==null && ((Map<?,?>)e.get("policy")).get("probe").equals(false));
        assertThat(runtime.endpointStatus).isEqualTo("complete");
    }
    @Test void realHttpJdbcAndTraceEventsPreserveResponse()throws Exception{
        var response=get("http://127.0.0.1:"+port+"/records/42",null);
        assertThat(response.statusCode()).isEqualTo(200);assertThat(response.body()).contains("42");
        assertThat(runtime.events()).anyMatch(e->e.get("kind").equals("http_request") && SdkRuntime.castPayload(e).get("route").equals("/records/{id}"));
        assertThat(runtime.events()).anyMatch(e->e.get("kind").equals("sql_query"));
        assertThat(runtime.events()).anyMatch(e->e.get("kind").equals("trace_span"));
    }
    @Test void sdkPrivateAuthenticationRateLimitAndMetrics()throws Exception{
        runtime.readToken="unit-private-reader";
        assertThat(get(runtime.sdkOrigin+"/_aiops/capabilities",null).statusCode()).isEqualTo(401);
        assertThat(get(runtime.sdkOrigin+"/_aiops/capabilities",runtime.readToken).statusCode()).isEqualTo(200);
        assertThat(get(runtime.sdkOrigin+"/_aiops/snapshot",runtime.readToken).statusCode()).isEqualTo(200);
        var limited=get(runtime.sdkOrigin+"/_aiops/snapshot",runtime.readToken);
        assertThat(limited.statusCode()).isEqualTo(429);assertThat(limited.headers().firstValue("Retry-After")).contains("2");
        assertThat(get(runtime.sdkOrigin+"/_aiops/probe-observations?run_id=invalid",runtime.readToken).statusCode()).isEqualTo(422);
        assertThat(get(runtime.sdkOrigin+"/metrics",null).statusCode()).isEqualTo(200);
    }
    @SpringBootConfiguration @EnableAutoConfiguration
    static class Application {
        @Bean Controller controller(JdbcTemplate jdbc){return new Controller(jdbc);}
        @Bean ThreadPoolTaskExecutor unitPool(){var pool=new ThreadPoolTaskExecutor();pool.setCorePoolSize(1);pool.setMaxPoolSize(1);pool.setQueueCapacity(1);return pool;}
    }
    @RestController static class Controller {
        final JdbcTemplate jdbc;Controller(JdbcTemplate jdbc){this.jdbc=jdbc;}
        @GetMapping("/records/{id}") Map<String,Object> record(@PathVariable long id){return jdbc.queryForMap("SELECT ? AS id",id);}
    }
}
