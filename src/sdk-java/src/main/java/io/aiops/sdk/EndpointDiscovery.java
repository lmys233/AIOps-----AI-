package io.aiops.sdk;

import java.lang.reflect.*;
import java.util.*;
import org.springframework.core.MethodParameter;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

final class EndpointDiscovery {
    private EndpointDiscovery(){}
    static List<Map<String,Object>> discover(RequestMappingHandlerMapping mapping,SdkProperties properties){
        List<Map<String,Object>> rows=new ArrayList<>();
        mapping.getHandlerMethods().forEach((info,handler)->{
            for(String path:info.getPatternValues()){
                if(path.equals("/error") || path.startsWith("/_aiops/"))continue;
                if(path.length()>512 || path.contains("//") || path.contains("..") || path.contains("\\") || path.contains("?") || path.contains("#") || path.contains("*") || path.contains("://"))
                    throw new IllegalArgumentException("Unsupported route pattern");
                Set<RequestMethod> methods=info.getMethodsCondition().getMethods();
                if(methods.isEmpty())methods=EnumSet.allOf(RequestMethod.class);
                for(RequestMethod method:methods){
                    if(method==RequestMethod.TRACE)continue;
                    List<Map<String,Object>> parameters=new ArrayList<>();Map<String,Object> body=null;
                    for(MethodParameter argument:handler.getMethodParameters()){
                        argument.initParameterNameDiscovery(new org.springframework.core.DefaultParameterNameDiscoverer());
                        PathVariable pathVariable=argument.getParameterAnnotation(PathVariable.class);
                        RequestParam query=argument.getParameterAnnotation(RequestParam.class);
                        if(pathVariable!=null || query!=null){
                            String name=pathVariable!=null ? SdkRuntime.first(pathVariable.name(),pathVariable.value(),argument.getParameterName()) :
                                SdkRuntime.first(query.name(),query.value(),argument.getParameterName());
                            if(!name.matches("[A-Za-z_][\\w.-]{0,63}"))throw new IllegalArgumentException("Unsupported parameter name");
                            parameters.add(Wire.map("name",name,"in",pathVariable!=null?"path":"query","required",pathVariable!=null || query.required() && query.defaultValue().equals(ValueConstants.DEFAULT_NONE),
                                "schema",schema(argument.getGenericParameterType(),new HashSet<>(),0)));
                        }
                        RequestBody requestBody=argument.getParameterAnnotation(RequestBody.class);
                        if(requestBody!=null){body=schema(argument.getGenericParameterType(),new HashSet<>(),0);body.put("x-aiops-required",requestBody.required());}
                    }
                    var policy=properties.routes().get(method+" "+path);
                    Map<String,Object> policyWire=policy==null ? Wire.map("effect","unknown","probe",false,"load",false,"max_requests",20,"max_concurrency",4,"max_duration_sec",10,"max_rps",5) : policy.wire();
                    String description=SdkRuntime.first(info.getName(),handler.getMethod().getName());
                    rows.add(Wire.map("id","ep_"+Wire.sha(method+" "+path).substring(0,24),"method",method.name(),"path",path,
                        "description",Wire.safe(description,1000),"parameters",parameters,"body_schema",body,"policy",policyWire));
                    if(rows.size()>256)throw new IllegalArgumentException("Endpoint inventory exceeds 256");
                }
            }
        });
        rows.sort(Comparator.comparing(r->r.get("method")+" "+r.get("path")));
        if(rows.stream().map(r->r.get("id")).distinct().count()!=rows.size())throw new IllegalArgumentException("Duplicate endpoint identity");
        return rows;
    }
    private static Map<String,Object> schema(Type type,Set<Type> stack,int depth){
        if(depth>8 || !stack.add(type))return Wire.map("type","object");
        try {
            if(type instanceof ParameterizedType parameterized && parameterized.getRawType() instanceof Class<?> raw){
                if(raw==Optional.class)return schema(parameterized.getActualTypeArguments()[0],stack,depth+1);
                if(Collection.class.isAssignableFrom(raw))return Wire.map("type","array","items",schema(parameterized.getActualTypeArguments()[0],stack,depth+1));
                if(Map.class.isAssignableFrom(raw))return Wire.map("type","object");
            }
            if(!(type instanceof Class<?> cls))return Wire.map("type","object");
            if(cls==String.class || cls==char.class || cls==Character.class || cls==UUID.class || cls.getName().startsWith("java.time."))return Wire.map("type","string");
            if(cls==boolean.class || cls==Boolean.class)return Wire.map("type","boolean");
            if(Set.of(int.class,long.class,short.class,byte.class,Integer.class,Long.class,Short.class,Byte.class).contains(cls))return Wire.map("type","integer");
            if(cls.isPrimitive() || Number.class.isAssignableFrom(cls))return Wire.map("type","number");
            if(cls.isEnum())return Wire.map("type","string","enum",Arrays.stream(cls.getEnumConstants()).map(Object::toString).toList());
            if(cls.isArray())return Wire.map("type","array","items",schema(cls.getComponentType(),stack,depth+1));
            var properties=new LinkedHashMap<String,Object>();
            var javaType=Wire.JSON.constructType(cls);
            for(var property:Wire.JSON.getDeserializationConfig().introspect(javaType).findProperties()){
                if(!property.couldDeserialize())continue;
                if(properties.size()>=32)throw new IllegalArgumentException("Body schema too large");
                properties.put(property.getName(),schema(property.getPrimaryType().getRawClass(),stack,depth+1));
            }
            return Wire.map("type","object","properties",properties);
        } finally {stack.remove(type);}
    }
}
