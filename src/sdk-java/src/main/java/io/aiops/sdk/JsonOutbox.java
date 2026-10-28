package io.aiops.sdk;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/** Owned by the export worker; atomic bounded snapshots, no I/O on request threads. */
final class JsonOutbox {
    private final Path path;private final int limit;private final LinkedHashMap<String,Map<String,Object>> rows=new LinkedHashMap<>();
    long dropped;
    JsonOutbox(Path path,int limit)throws Exception{
        this.path=path;this.limit=limit;
        if(Files.exists(path)){
            if(Files.size(path)>8_000_000)throw new IllegalArgumentException("Outbox size limit");
            @SuppressWarnings("unchecked") List<Map<String,Object>> saved=Wire.JSON.readValue(Files.readString(path),List.class);
            for(var row:saved){if(rows.size()>=limit)break;rows.put((String)row.get("event_id"),row);}
        }
    }
    void add(List<Map<String,Object>> events)throws Exception{
        boolean changed=false;
        for(var row:events){String key=(String)row.get("event_id");if(rows.containsKey(key))continue;
            if(rows.size()>=limit){String oldest=rows.keySet().stream().filter(id->{var r=rows.get(id);return !SdkRuntime.important((String)r.get("kind"),SdkRuntime.castPayload(r));})
                .findFirst().orElse(rows.keySet().iterator().next());rows.remove(oldest);dropped++;}rows.put(key,row);changed=true;}
        if(changed)save();
    }
    List<Map<String,Object>> batch(){return rows.values().stream().limit(32).toList();}
    void ack(List<String> ids)throws Exception{ids.forEach(rows::remove);save();}
    private void save()throws Exception{
        Files.createDirectories(path.getParent());String json=Wire.json(new ArrayList<>(rows.values()));
        while(json.getBytes(StandardCharsets.UTF_8).length>8_000_000 && !rows.isEmpty()){rows.remove(rows.keySet().iterator().next());dropped++;json=Wire.json(new ArrayList<>(rows.values()));}
        Path temporary=path.resolveSibling(path.getFileName()+".tmp");Files.writeString(temporary,json,StandardCharsets.UTF_8);
        try{Files.move(temporary,path,StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE);}
        catch(AtomicMoveNotSupportedException e){Files.move(temporary,path,StandardCopyOption.REPLACE_EXISTING);}
    }
}
