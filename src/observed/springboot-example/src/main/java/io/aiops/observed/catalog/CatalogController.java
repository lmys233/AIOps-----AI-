package io.aiops.observed.catalog;

import java.util.*;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

/** The business code does not import the SDK; removing its dependency preserves these routes. */
@RestController
public class CatalogController {
    private final JdbcTemplate database;
    CatalogController(JdbcTemplate database){this.database=database;}
    @GetMapping(path="/catalog/items",name="查询商品目录")
    public List<Map<String,Object>> items(){return database.queryForList("SELECT id,name,price FROM java_catalog_items ORDER BY id");}
    @GetMapping(path="/catalog/items/{id}",name="查询某个商品")
    public ResponseEntity<Map<String,Object>> item(@PathVariable long id){
        var rows=database.queryForList("SELECT id,name,price FROM java_catalog_items WHERE id=?",id);
        return rows.isEmpty()?ResponseEntity.notFound().build():ResponseEntity.ok(rows.get(0));
    }
    @PostMapping(path="/catalog/items",name="新增商品（写操作）")
    public Map<String,Object> create(@RequestBody Item item){
        if(item.name()==null || item.name().isBlank() || item.name().length()>80 || !Double.isFinite(item.price()) || item.price()<0)
            throw new org.springframework.web.server.ResponseStatusException(org.springframework.http.HttpStatus.BAD_REQUEST,"Invalid item");
        database.update("INSERT INTO java_catalog_items(id,name,price) VALUES(?,?,?)",item.id(),item.name(),item.price());
        LoggerFactory.getLogger(getClass()).info("Catalog item created");return Map.of("id",item.id(),"created",true);
    }
    public record Item(long id,String name,double price){}
}
