package io.aiops.sdk;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Language-neutral protocol; no platform classes or application dependencies. */
public final class Wire {
    public static final ObjectMapper JSON=new ObjectMapper();
    public static final String VERSION="0.3.0-java";
    public static final Map<String,String> CAPABILITIES=Map.of(
        "health","/health","metrics","/metrics","services","/services",
        "capabilities","/_aiops/capabilities","snapshot","/_aiops/snapshot",
        "probe_observations","/_aiops/probe-observations","changes","/changes");
    private Wire() {}
    public static Map<String,Object> map(Object... values) {
        Map<String,Object> result=new LinkedHashMap<>();
        for(int i=0;i<values.length;i+=2) result.put((String)values[i],values[i+1]);
        return result;
    }
    public static String id(){ return UUID.randomUUID().toString().replace("-",""); }
    public static String now(){ return Instant.now().toString(); }
    public static String json(Object value){
        try { return JSON.writeValueAsString(value); }
        catch(JsonProcessingException e){ throw new IllegalArgumentException("Invalid SDK JSON",e); }
    }
    public static String sha(String value){
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch(Exception e){ throw new IllegalStateException(e); }
    }
    /** Match Python json.dumps(ensure_ascii=False, sort_keys=True), including separators. */
    public static String canonical(Object value){
        if(value instanceof Map<?,?> map){
            List<String> keys=map.keySet().stream().map(Object::toString).sorted().toList();
            return "{"+String.join(", ",keys.stream().map(k->json(k)+": "+canonical(map.get(k))).toList())+"}";
        }
        if(value instanceof List<?> list) return "["+String.join(", ",list.stream().map(Wire::canonical).toList())+"]";
        return json(value);
    }
    public static String pullToken(String enrollment,Map<String,Object> identity){
        String text="sdk-pull-v1|"+String.join("|",List.of("tenant","observed_id","service","instance","boot_id").stream()
            .map(k->String.valueOf(identity.get(k))).toList());
        try {
            Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(enrollment.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(text.getBytes(StandardCharsets.UTF_8)));
        } catch(Exception e){throw new IllegalStateException(e);}
    }
    public static String identifier(String value){
        if(value==null || !value.matches("[\\p{L}\\p{N}_.:/-]{1,128}") || value.contains("://"))
            throw new IllegalArgumentException("Invalid SDK identity");
        return value;
    }
    public static String origin(String value){
        java.net.URI uri=java.net.URI.create(value);
        if(!Set.of("http","https").contains(uri.getScheme()) || uri.getHost()==null || uri.getUserInfo()!=null ||
            uri.getQuery()!=null || uri.getFragment()!=null || !(uri.getPath().isEmpty() || uri.getPath().equals("/")) ||
            Set.of("169.254.169.254","metadata.google.internal").contains(uri.getHost().toLowerCase(Locale.ROOT)))
            throw new IllegalArgumentException("SDK URL must be an HTTP origin without credentials");
        return value.replaceAll("/$","");
    }
    public static String safe(String value,int limit){
        if(value==null)return "";
        String text=value.replaceAll("(?i)(Bearer\\s+)[^\\s\"',;]+","$1***")
            .replaceAll("(?i)((?:password|passwd|token|secret|api[_-]?key|authorization)\\s*[=:]\\s*)[^\\s,;]+","$1***")
            .replaceAll("(?i)(jdbc:[^\\s]+|https?://[^\\s/@]+:[^\\s/@]+@[^\\s]+)","<connection-redacted>");
        return text.length()>limit ? text.substring(0,Math.max(0,limit-1))+"…" : text;
    }
}
