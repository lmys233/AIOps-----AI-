package io.aiops.sdk;

import java.lang.reflect.*;
import java.sql.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.sql.DataSource;
import org.aopalliance.intercept.MethodInterceptor;
import org.springframework.aop.framework.ProxyFactory;

final class JdbcObservation {
    private JdbcObservation(){}
    static Object wrap(DataSource target,String name,SdkRuntime runtime){
        if(Modifier.isFinal(target.getClass().getModifiers())){
            runtime.capability("sdk_sql","unsupported","Final concrete DataSource cannot be safely proxied","jdbc");return target;
        }
        String pool="pool-"+Wire.sha(name).substring(0,20);runtime.pools.put(pool,target);
        if(target.getClass().getName().startsWith("com.zaxxer.hikari.")){
            runtime.metrics.gauge("db_pool_in_use_connections",()->hikariValue(runtime,"getActiveConnections"));
            runtime.metrics.gauge("db_pool_waiting_connections",()->hikariValue(runtime,"getThreadsAwaitingConnection"));
            runtime.metrics.gauge("db_pool_max_connections",()->hikariValue(runtime,"getMaximumPoolSize"));
        }
        ProxyFactory proxy=new ProxyFactory(target);proxy.setProxyTargetClass(true);
        proxy.addAdvice((MethodInterceptor)invocation->{
            if(!invocation.getMethod().getName().equals("getConnection"))return invocation.proceed();
            long started=System.nanoTime();String at=Wire.now();Throwable failure=null;
            try {return connection((Connection)invocation.proceed(),pool,runtime);}
            catch(Throwable e){failure=e;throw e;}
            finally {
                double ms=(System.nanoTime()-started)/1e6;runtime.metrics.acquire(ms/1000);
                var payload=Wire.map("pool_id",pool,"elapsed_ms",ms,"outcome",failure==null?"success":"error","execution_started_at",at);
                if(failure!=null)payload.putAll(exception(failure));
                runtime.emit(failure instanceof SQLTransientConnectionException?"pool_timeout":"pool_acquire","sdk_pool",payload);
            }
        });
        Object wrapped=proxy.getProxy();
        runtime.capability("sdk_sql","available","Standard JDBC Statement/PreparedStatement; unwrap can bypass observation","jdbc-proxy");
        runtime.capability("sdk_pool","available","Acquisition and holding intervals; Hikari gauges when present","jdbc-proxy");
        runtime.capability("sdk_transaction","available","Explicit setAutoCommit/commit/rollback observation","jdbc-proxy");
        return wrapped;
    }
    private static Connection connection(Connection target,String pool,SdkRuntime runtime){
        String session=Wire.id();long acquired=System.nanoTime();var closed=new AtomicBoolean();String[] transaction={null};
        Map<String,String> holderContext=RequestContext.current();
        if(runtime.holders.size()<256)runtime.holders.put(session,Wire.map("pool_id",pool,"session_id",session,"acquired_at",Wire.now(),"phase","held",
            "request_id",holderContext.get("request_id"),"thread_id",String.valueOf(Thread.currentThread().getId())));
        return (Connection)Proxy.newProxyInstance(Connection.class.getClassLoader(),new Class<?>[]{Connection.class},(proxy,method,args)->{
            if(method.getName().equals("equals") && method.getParameterCount()==1)return proxy==args[0];
            if(method.getName().equals("hashCode") && method.getParameterCount()==0)return System.identityHashCode(proxy);
            String name=method.getName();long started=System.nanoTime();Throwable failure=null;
            try {
                Object value=invoke(target,method,args);
                if(value instanceof Statement statement){String sql=args!=null && args.length>0 && args[0] instanceof String ? (String)args[0]:null;return statement(statement,sql,session,pool,transaction,runtime,(Connection)proxy);}
                if(name.equals("setAutoCommit") && args!=null && Boolean.FALSE.equals(args[0]))transaction[0]=Wire.id();
                return value;
            } catch(Throwable e){failure=e;throw e;}
            finally {
                if(name.equals("close") && failure==null && closed.compareAndSet(false,true)){
                    runtime.holders.remove(session);runtime.emit("pool_release","sdk_pool",Wire.map("pool_id",pool,"session_id",session,"hold_ms",(System.nanoTime()-acquired)/1e6),holderContext,false);
                }
                if(Set.of("setAutoCommit","commit","rollback").contains(name)){
                    var context=RequestContext.current();context.put("session_id",session);if(transaction[0]!=null)context.put("transaction_id",transaction[0]);
                    runtime.emit("transaction_event","sdk_transaction",Wire.map("phase",name,"outcome",failure==null?name:"error","duration_ms",(System.nanoTime()-started)/1e6),context,false);
                    if(name.equals("commit") || name.equals("rollback") || name.equals("setAutoCommit") && Boolean.TRUE.equals(args[0]))transaction[0]=null;
                }
            }
        });
    }
    private static Statement statement(Statement target,String prepared,String session,String pool,String[] transaction,SdkRuntime runtime,Connection connection){
        Class<?> api=target instanceof CallableStatement?CallableStatement.class:target instanceof PreparedStatement?PreparedStatement.class:Statement.class;
        return (Statement)Proxy.newProxyInstance(api.getClassLoader(),new Class<?>[]{api},(proxy,method,args)->{
            if(method.getName().equals("equals") && method.getParameterCount()==1)return proxy==args[0];
            if(method.getName().equals("hashCode") && method.getParameterCount()==0)return System.identityHashCode(proxy);
            if(method.getName().equals("getConnection"))return connection;
            if(!method.getName().startsWith("execute"))return invoke(target,method,args);
            String sql=prepared!=null?prepared:args!=null && args.length>0 && args[0] instanceof String?(String)args[0]:"<batch>";
            String shape=SqlShape.of(sql),at=Wire.now();long started=System.nanoTime();Throwable failure=null;
            phase(runtime,session,"database_query");
            try {return invoke(target,method,args);}catch(Throwable e){failure=e;throw e;}
            finally {
                var payload=Wire.map("pool_id",pool,"query_shape",shape,"query_fingerprint",Wire.sha(shape).substring(0,24),
                    "execution_ms",(System.nanoTime()-started)/1e6,"execution_started_at",at,"outcome",failure==null?"success":"error",
                    "purpose","business","database_identity",null);
                if(failure!=null){
                    payload.putAll(exception(failure));
                    var match=java.util.regex.Pattern.compile("(?i)(?:relation|table|column)\\s+[\"']([^\"']+)[\"']").matcher(String.valueOf(payload.get("message")));
                    if(match.find())payload.put("object_name",match.group(1));
                }
                var context=RequestContext.current();context.put("session_id",session);if(transaction[0]!=null)context.put("transaction_id",transaction[0]);
                runtime.emit(failure==null?"sql_query":"sql_error","sdk_sql",payload,context,false);
                phase(runtime,session,"held");
            }
        });
    }
    static Object invoke(Object target,Method method,Object[] args)throws Throwable{
        try{return method.invoke(target,args);}catch(InvocationTargetException e){throw e.getCause();}
    }
    static Map<String,Object> exception(Throwable error){
        Throwable root=error;for(int i=0;i<16 && root.getCause()!=null && root.getCause()!=root;i++)root=root.getCause();
        String message=Wire.safe(root.getMessage(),8000);
        // JDBC drivers can embed SQL and literal parameter values in their exception text.
        if(root instanceof SQLException)message=message.replaceFirst("(?is)\\s*(?:;?\\s*SQL statement:|\\n\\s*SQL:).*","")
            .replaceAll("(?is)(Key \\([^\\n]+\\)=)\\([^\\n]+\\)","$1(<values-redacted>)");
        String stack=Arrays.stream(root.getStackTrace()).limit(32).map(StackTraceElement::toString).reduce("",(a,b)->a+b+"\n");
        return Wire.map("exception_type",root.getClass().getSimpleName(),"sqlstate",root instanceof SQLException sql?sql.getSQLState():null,
            "message",message,"stack",Wire.safe(stack,8000));
    }
    private static void phase(SdkRuntime runtime,String session,String phase){
        runtime.holders.computeIfPresent(session,(key,old)->{var copy=new LinkedHashMap<>(old);copy.put("phase",phase);return copy;});
    }
    private static double hikariValue(SdkRuntime runtime,String method){
        double total=0;
        for(DataSource pool:runtime.pools.values())if(pool.getClass().getName().startsWith("com.zaxxer.hikari.")){
            try {
                if(method.equals("getMaximumPoolSize"))total+=((Number)pool.getClass().getMethod(method).invoke(pool)).doubleValue();
                else {Object bean=pool.getClass().getMethod("getHikariPoolMXBean").invoke(pool);if(bean!=null)total+=((Number)bean.getClass().getMethod(method).invoke(bean)).doubleValue();}
            } catch(Exception ignored){return Double.NaN;}
        }
        return total;
    }
}
