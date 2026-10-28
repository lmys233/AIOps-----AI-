package io.aiops.sdk;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

final class ProbeCorrelation {
    private ProbeCorrelation(){}
    static boolean valid(String token,String run,String method,String path,String request,String signature){
        // Wire protocol implemented after cross-language fixture comparison; absent means ordinary traffic.
        if(token==null || token.isEmpty() || run==null || !run.matches("probe_[a-f0-9]{32}") ||
            request==null || !request.matches("[a-f0-9]{32}") || signature==null || !signature.matches("[a-f0-9]{64}"))return false;
        try {
            Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(token.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));
            String text=run+":"+request;
            return MessageDigest.isEqual(HexFormat.of().formatHex(mac.doFinal(text.getBytes(StandardCharsets.UTF_8))).getBytes(StandardCharsets.US_ASCII),signature.getBytes(StandardCharsets.US_ASCII));
        } catch(Exception ignored){return false;}
    }
}
