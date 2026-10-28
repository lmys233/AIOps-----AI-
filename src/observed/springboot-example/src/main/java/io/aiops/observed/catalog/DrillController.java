package io.aiops.observed.catalog;

import java.util.*;
import java.util.concurrent.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.client.RestTemplate;

/** Explicit local drills; absent from production routing unless enabled. */
@RestController
@ConditionalOnProperty(name="demo.drills-enabled",havingValue="true")
class DrillController {
    private final JdbcTemplate database;private final ThreadPoolTaskExecutor worker;private final RestTemplate client;
    private final int port;
    DrillController(JdbcTemplate database,ThreadPoolTaskExecutor catalogWorker,RestTemplate downstreamClient,@Value("${server.port}") int port){
        this.database=database;worker=catalogWorker;client=downstreamClient;this.port=port;
    }
    @GetMapping(path="/drills/missing-table",name="演练：数据库对象缺失（只读）")
    Object missing(){return database.queryForList("SELECT id FROM java_catalog_missing_table");}
    @GetMapping(path="/drills/slow-request",name="演练：慢HTTP请求")
    Object slow(@RequestParam(defaultValue="1200") int delayMs)throws InterruptedException{
        if(delayMs<0 || delayMs>3000)throw new IllegalArgumentException("Delay outside drill bounds");Thread.sleep(delayMs);return Map.of("delay_ms",delayMs);
    }
    @GetMapping(path="/drills/server-error",name="演练：应用异常")
    Object error(){throw new IllegalStateException("Deliberate isolated Java example failure");}
    @GetMapping(path="/drills/downstream",name="演练：本服务内部HTTP调用")
    Object downstream(){return client.getForObject("http://127.0.0.1:"+port+"/catalog/items",Object.class);}
    @PostMapping(path="/drills/worker-rejection",name="演练：有界线程池拒绝（制造流量）")
    Object rejected()throws Exception{
        var entered=new CountDownLatch(1);var release=new CountDownLatch(1);boolean rejected=false;
        try {
            worker.execute(()->{entered.countDown();try{release.await(2,TimeUnit.SECONDS);}catch(InterruptedException e){Thread.currentThread().interrupt();}});
            if(!entered.await(1,TimeUnit.SECONDS))throw new IllegalStateException("Drill worker did not start");
            worker.execute(()->{});
            try{worker.execute(()->{});}catch(RejectedExecutionException expected){rejected=true;}
            return Map.of("rejected",rejected);
        } finally {release.countDown();}
    }
}
