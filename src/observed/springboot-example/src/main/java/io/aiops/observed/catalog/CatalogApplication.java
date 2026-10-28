package io.aiops.observed.catalog;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.client.RestTemplate;

@SpringBootApplication
public class CatalogApplication {
    public static void main(String[] args){SpringApplication.run(CatalogApplication.class,args);}
    @Bean ThreadPoolTaskExecutor catalogWorker(){
        var pool=new ThreadPoolTaskExecutor();pool.setCorePoolSize(1);pool.setMaxPoolSize(1);pool.setQueueCapacity(1);pool.setThreadNamePrefix("catalog-worker-");return pool;
    }
    @Bean RestTemplate downstreamClient(){return new RestTemplate();}
}
