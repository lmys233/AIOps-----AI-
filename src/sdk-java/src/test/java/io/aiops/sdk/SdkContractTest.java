package io.aiops.sdk;

import static org.assertj.core.api.Assertions.*;
import java.nio.file.*;
import java.sql.*;
import java.util.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.springframework.boot.context.properties.bind.*;
import org.springframework.boot.context.properties.source.ConfigurationPropertySources;
import org.springframework.core.env.*;
import com.zaxxer.hikari.HikariDataSource;

class SdkContractTest {
    static SdkProperties properties(Map<String,Object> extra){
        var values=new LinkedHashMap<String,Object>();values.put("aiops.sdk.port",0);values.put("aiops.sdk.service-name","renamed-java");
        values.put("aiops.sdk.observed-name","renamed-system");values.put("aiops.sdk.env","dev");values.put("aiops.sdk.success-sample-every",1);values.putAll(extra);
        return new Binder(ConfigurationPropertySources.from(new MapPropertySource("unit",values))).bind("aiops.sdk",Bindable.of(SdkProperties.class)).get();
    }
    static SdkRuntime runtime(){return new SdkRuntime(properties(Map.of()),new MockEnvironmentForTest());}
    static final class MockEnvironmentForTest extends StandardEnvironment{}
    @Test void canonicalPythonSeparatorsUnicodeAndNull(){
        assertThat(Wire.canonical(Wire.map("z",null,"a",List.of("中文",1,true)))).isEqualTo("{\"a\": [\"中文\", 1, true], \"z\": null}");
    }
    @Test void credentialsAndSignedProbeMatchSharedAlgorithm()throws Exception{
        var runtime=runtime();runtime.bind("tenant-x","obs_"+"a".repeat(32),"unit-enrollment");
        assertThat(runtime.readToken).hasSize(64);
        String run="probe_"+"b".repeat(32),request="c".repeat(32);
        var mac=javax.crypto.Mac.getInstance("HmacSHA256");mac.init(new javax.crypto.spec.SecretKeySpec(runtime.readToken.getBytes(),"HmacSHA256"));
        String proof=HexFormat.of().formatHex(mac.doFinal((run+":"+request).getBytes()));
        var valid=RequestContext.inbound(null,run,runtime.readToken,"GET","/items",request,proof);
        assertThat(valid).containsEntry("probe_id",run).containsEntry("request_id",request);
        assertThat(RequestContext.inbound(null,run,runtime.readToken,"GET","/items",request,"0".repeat(64))).doesNotContainKey("probe_id");
    }
    @Test void registrationPromotesOnlyCurrentUnboundBufferedEvents(){
        var runtime=runtime();runtime.emit("application_log","sdk_log",Wire.map("msg","startup","level","INFO"));
        runtime.bind("test-tenant","obs_"+"d".repeat(32),"unit-enrollment");
        assertThat(runtime.events().get(0)).containsEntry("schema_version",2);
        assertThat(((Map<?,?>)runtime.events().get(0).get("identity")).get("observed_id")).isEqualTo("obs_"+"d".repeat(32));
        assertThat(runtime.identity()).containsEntry("cluster",null);
    }
    @Test void boundedSamplingAndCriticalRetention(){
        var runtime=new SdkRuntime(properties(Map.of("aiops.sdk.buffer-capacity",32,"aiops.sdk.success-sample-every",10)),new StandardEnvironment());
        for(int i=0;i<100;i++)runtime.emit("http_request","sdk_http",Wire.map("outcome","complete","status",200));
        assertThat(runtime.sampled.get()).isGreaterThan(0);
        for(int i=0;i<40;i++)runtime.emit("sql_error","sdk_sql",Wire.map("outcome","error","message","missing"));
        assertThat(runtime.events()).hasSize(32);assertThat(runtime.dropped.get()).isPositive();
        assertThat(runtime.events()).allMatch(row->row.get("kind").equals("sql_error"));
    }
    @Test void sqlShapeDoesNotRecordParametersCommentsOrDollarQuotedLiterals(){
        String shape=SqlShape.of("select 'private-value', $$token-value$$, 72 from \"table_42\" /* secret */ -- comment\nwhere id=4");
        assertThat(shape).doesNotContain("private-value","token-value","secret","72").contains("\"table_42\"","?");
        assertThat(SqlShape.of("select 'unterminated")).isEqualTo("<unparsed>");
        assertThat(Wire.safe("Authorization=secretvalue password=x Bearer tokenvalue",1000)).doesNotContain("secretvalue","tokenvalue","password=x");
    }
    @Test void slowSuccessesAndErrorSpansAreNotLostToSuccessSampling(){
        var runtime=new SdkRuntime(properties(Map.of("aiops.sdk.success-sample-every",1000)),new StandardEnvironment());
        for(int n=0;n<5;n++){
            assertThat(runtime.emit("http_request","sdk_http",Wire.map("outcome","complete","total_ms",1100,"status",200))).isNotNull();
            assertThat(runtime.emit("sql_query","sdk_sql",Wire.map("outcome","success","execution_ms",600))).isNotNull();
            String trace=Wire.id(),span=Wire.id().substring(0,16);
            assertThat(runtime.emit("trace_span","sdk_trace",Wire.map("status","ERROR","name","error-span","kind","SERVER","span_id",span,
                "parent_span_id",null,"start_ns",0L,"end_ns",1L,"attributes",Map.of()),Map.of("trace_id",trace,"span_id",span),false)).isNotNull();
        }
    }
    @Test void jdbcWrapperRetainsConcreteTypeUnwrapSqlstateAndTransactionSemantics()throws Exception{
        var runtime=runtime();var raw=new HikariDataSource();raw.setJdbcUrl("jdbc:h2:mem:unit_"+Wire.id());raw.setMaximumPoolSize(2);
        try {
            DataSource observed=(DataSource)JdbcObservation.wrap(raw,"sample",runtime);
            assertThat(observed).isInstanceOf(HikariDataSource.class);
            try(var connection=observed.getConnection()){
                assertThat(connection.equals(connection)).isTrue();
                assertThat(connection.unwrap(Connection.class)).isNotNull();connection.setAutoCommit(false);
                try(var statement=connection.createStatement()){
                    statement.execute("CREATE TABLE ledger (id INT)");statement.execute("INSERT INTO ledger VALUES(1)");connection.rollback();
                    try(var result=statement.executeQuery("SELECT COUNT(*) FROM ledger")){result.next();assertThat(result.getInt(1)).isZero();}
                    assertThatThrownBy(()->statement.executeQuery("SELECT * FROM missing_unit_table")).isInstanceOf(SQLException.class);
                }
            }
            assertThat(runtime.holders).isEmpty();
            assertThat(runtime.events()).anyMatch(row->row.get("kind").equals("sql_error") && SdkRuntime.castPayload(row).get("sqlstate")!=null);
            assertThat(runtime.events()).anyMatch(row->row.get("kind").equals("transaction_event"));
            assertThat(runtime.metrics.scrape()).contains("db_pool_in_use_connections","db_pool_acquire_seconds_bucket");
        } finally {raw.close();}
    }
    @Test void outboxReloadAndExactAcknowledgmentDoNotLosePendingRecords()throws Exception{
        Path folder=Files.createTempDirectory("sdk-outbox-");Path path=folder.resolve("outbox.json");
        try {
            var runtime=runtime();runtime.bind("test","obs_"+"a".repeat(32),"unit-enrollment");
            var row=runtime.emit("sql_error","sdk_sql",Wire.map("message","missing","sqlstate","42P01"));
            var box=new JsonOutbox(path,32);box.add(List.of(row));var restarted=new JsonOutbox(path,32);
            assertThat(restarted.batch()).hasSize(1);restarted.ack(List.of((String)row.get("event_id")));
            assertThat(new JsonOutbox(path,32).batch()).isEmpty();
        } finally {Files.deleteIfExists(path);Files.delete(folder);}
    }
    @Test void w3cInputAndContextAreRestored(){
        String parent="00-"+"a".repeat(32)+"-"+"b".repeat(16)+"-01";
        var context=RequestContext.inbound(parent,null,"","GET","/items",null,null);
        assertThat(context).containsEntry("trace_id","a".repeat(32)).containsEntry("parent_span_id","b".repeat(16));
        try(var ignored=RequestContext.bind(context)){assertThat(RequestContext.current()).containsKey("request_id");}
        assertThat(RequestContext.current()).isEmpty();
    }
}
