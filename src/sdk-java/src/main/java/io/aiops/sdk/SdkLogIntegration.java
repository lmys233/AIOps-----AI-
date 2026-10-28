package io.aiops.sdk;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.ApplicationListener;

final class SdkLogIntegration implements ApplicationListener<ApplicationReadyEvent>,AutoCloseable {
    private final SdkRuntime runtime;private Logger root;private SdkLogAppender appender;
    SdkLogIntegration(SdkRuntime runtime){this.runtime=runtime;}
    @Override public void onApplicationEvent(ApplicationReadyEvent event){
        if(LoggerFactory.getILoggerFactory() instanceof LoggerContext context){
            root=context.getLogger(Logger.ROOT_LOGGER_NAME);appender=new SdkLogAppender(runtime);appender.setContext(context);appender.start();root.addAppender(appender);
            runtime.capability("sdk_log","available","Application appenders are retained; scoped events use existing platform Loki relay","logback-side-channel");
        }
    }
    @Override public void close(){if(root!=null && appender!=null){root.detachAppender(appender);appender.stop();}}
}
