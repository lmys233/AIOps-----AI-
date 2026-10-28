package io.aiops.sdk;

import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.boot.autoconfigure.condition.*;
import org.springframework.boot.autoconfigure.web.servlet.WebMvcAutoConfiguration;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.boot.web.client.RestClientCustomizer;
import org.springframework.boot.web.context.WebServerInitializedEvent;
import org.springframework.context.ApplicationListener;
import org.springframework.context.annotation.*;
import org.springframework.core.Ordered;
import org.springframework.core.env.Environment;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

@AutoConfiguration(after=WebMvcAutoConfiguration.class)
@ConditionalOnWebApplication(type=ConditionalOnWebApplication.Type.SERVLET)
@ConditionalOnProperty(prefix="aiops.sdk",name="enabled",havingValue="true",matchIfMissing=true)
@EnableConfigurationProperties(SdkProperties.class)
public class AiopsAutoConfiguration {
    @Bean @ConditionalOnMissingBean SdkRuntime aiopsRuntime(SdkProperties properties,Environment environment){
        if(properties.observedName()==null && environment.getProperty("observed-name")!=null){
            properties=new SdkProperties(properties.enabled(),environment.getProperty("observed-name"),properties.serviceName(),properties.env(),properties.cluster(),
                properties.instance(),properties.bindAddress(),properties.port(),properties.advertiseUrl(),properties.businessUrl(),properties.registrationUrl(),properties.tokenEnv(),properties.diagnosticsTokenEnv(),
                properties.stateDir(),properties.bufferCapacity(),properties.successSampleEvery(),properties.jdbcEnabled(),properties.workersEnabled(),properties.logsEnabled(),properties.routes());
        }
        return new SdkRuntime(properties,environment);
    }
    @Bean static ObservationPostProcessor aiopsObservationPostProcessor(ObjectProvider<SdkRuntime> runtime){return new ObservationPostProcessor(runtime);}
    @Bean FilterRegistrationBean<HttpObservationFilter> aiopsHttpFilter(SdkRuntime runtime){
        var registration=new FilterRegistrationBean<>(new HttpObservationFilter(runtime));registration.setOrder(Ordered.HIGHEST_PRECEDENCE+20);
        registration.setAsyncSupported(true);return registration;
    }
    @Bean SdkLifecycle aiopsLifecycle(SdkRuntime runtime,@Qualifier("requestMappingHandlerMapping") RequestMappingHandlerMapping mapping){return new SdkLifecycle(runtime,mapping);}
    @Bean ApplicationListener<WebServerInitializedEvent> aiopsPortListener(SdkLifecycle lifecycle){return lifecycle::webServer;}
    @Bean RestClientCustomizer aiopsRestClientCustomizer(SdkRuntime runtime){
        return builder->{builder.requestInterceptor(new DownstreamObservation(runtime));runtime.capability("downstream_http","partial","Spring-managed RestClient.Builder; manually created clients are not mounted","rest-client-customizer");};
    }
    @Configuration(proxyBeanMethods=false)
    @ConditionalOnClass(name="ch.qos.logback.classic.LoggerContext")
    static class LogbackConfiguration {
        @Bean @ConditionalOnProperty(prefix="aiops.sdk",name="logs-enabled",havingValue="true",matchIfMissing=true)
        SdkLogIntegration aiopsLogIntegration(SdkRuntime runtime){return new SdkLogIntegration(runtime);}
    }
}
