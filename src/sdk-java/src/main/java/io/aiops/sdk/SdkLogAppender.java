package io.aiops.sdk;

import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.AppenderBase;

/** Side-channel log capture; the application's appenders/formatters remain installed. */
final class SdkLogAppender extends AppenderBase<ILoggingEvent> {
    private final SdkRuntime runtime;
    SdkLogAppender(SdkRuntime runtime){this.runtime=runtime;setName("AIOPS_SDK");}
    @Override protected void append(ILoggingEvent event){
        if(event.getLoggerName().startsWith("io.aiops.sdk"))return;
        try {
            String level=event.getLevel().toString();if(level.equals("WARN"))level="WARNING";
            var payload=Wire.map("level",level,"msg",Wire.safe(event.getFormattedMessage(),8000),"error_type","");
            if(event.getThrowableProxy()!=null){
                payload.put("exception_type",event.getThrowableProxy().getClassName());
                payload.put("message",Wire.safe(event.getThrowableProxy().getMessage(),8000));
            }
            if(level.equals("ERROR"))payload.put("outcome","exception");
            runtime.emit("application_log","sdk_log",payload);
        } catch(Exception ignored){runtime.dropped.incrementAndGet();}
    }
}
