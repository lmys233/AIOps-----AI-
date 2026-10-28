package io.aiops.sdk;

final class SqlShape {
    private SqlShape(){}
    static String of(String sql){
        if(sql==null)return "<unknown>";
        if(sql.length()>20000)return "<oversize>";
        StringBuilder out=new StringBuilder();int i=0;
        while(i<sql.length()){
            if(sql.startsWith("--",i)){int end=sql.indexOf('\n',i);i=end<0?sql.length():end;out.append(' ');}
            else if(sql.startsWith("/*",i)){
                int depth=1;i+=2;
                while(i<sql.length() && depth>0){if(sql.startsWith("/*",i)){depth++;i+=2;}else if(sql.startsWith("*/",i)){depth--;i+=2;}else i++;}
                if(depth>0)return "<unparsed>";out.append(' ');
            } else if(sql.charAt(i)=='\''){
                i++;boolean closed=false;
                while(i<sql.length()){
                    if(sql.charAt(i)=='\\'){i+=2;continue;}
                    if(sql.charAt(i)=='\''){i++;if(i<sql.length() && sql.charAt(i)=='\''){i++;continue;}closed=true;break;}i++;
                }
                if(!closed)return "<unparsed>";out.append('?');
            } else if(sql.charAt(i)=='$'){
                var match=java.util.regex.Pattern.compile("\\$(?:[A-Za-z_][\\w]*)?\\$").matcher(sql.substring(i));
                if(match.lookingAt()){
                    String tag=match.group();int end=sql.indexOf(tag,i+tag.length());if(end<0)return "<unparsed>";
                    i=end+tag.length();out.append('?');
                } else out.append(sql.charAt(i++));
            } else if(sql.charAt(i)=='"'){
                int start=i++;boolean closed=false;
                while(i<sql.length()){if(sql.charAt(i++)=='"'){if(i<sql.length() && sql.charAt(i)=='"'){i++;continue;}closed=true;break;}}
                if(!closed)return "<unparsed>";out.append(sql,start,i);
            } else if(Character.isDigit(sql.charAt(i)) && (i==0 || !Character.isJavaIdentifierPart(sql.charAt(i-1)))){
                while(i<sql.length() && (Character.isDigit(sql.charAt(i)) || sql.charAt(i)=='.'))i++;out.append('?');
            } else out.append(sql.charAt(i++));
        }
        String text=out.toString().replaceAll("\\s+"," ").trim();return text.length()>2000?text.substring(0,2000):text;
    }
}
