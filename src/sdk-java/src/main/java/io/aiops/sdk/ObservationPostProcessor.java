package io.aiops.sdk;

import javax.sql.DataSource;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.client.RestTemplate;

final class ObservationPostProcessor implements BeanPostProcessor {
    private final ObjectProvider<SdkRuntime> provider;
    ObservationPostProcessor(ObjectProvider<SdkRuntime> provider){this.provider=provider;}
    @Override public Object postProcessAfterInitialization(Object bean,String name){
        if(bean instanceof DataSource || bean instanceof ThreadPoolTaskExecutor || bean instanceof java.util.concurrent.ThreadPoolExecutor || bean instanceof RestTemplate){
            SdkRuntime runtime=provider.getIfAvailable();if(runtime==null)return bean;
            try {
                if(bean instanceof DataSource datasource && runtime.config.jdbcEnabled())return JdbcObservation.wrap(datasource,name,runtime);
                if((bean instanceof ThreadPoolTaskExecutor || bean instanceof java.util.concurrent.ThreadPoolExecutor) && runtime.config.workersEnabled())return WorkerObservation.wrap(bean,name,runtime);
                if(bean instanceof RestTemplate rest){
                    var interceptors=new java.util.ArrayList<>(rest.getInterceptors());interceptors.add(new DownstreamObservation(runtime));rest.setInterceptors(interceptors);
                    runtime.capability("downstream_http","available","Spring-managed RestTemplate; manually created clients are not mounted","rest-template");
                }
            } catch(Exception error){runtime.capability(bean instanceof DataSource?"sdk_sql":"sdk_worker","unsupported","Adapter mounting failed: "+error.getClass().getSimpleName(),"spring-bpp");}
        }
        return bean;
    }
}
