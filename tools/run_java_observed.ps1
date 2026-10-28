param([string]$EnrollmentFile='', [switch]$Drills)
$ErrorActionPreference='Stop'
$workspace=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$jar=Join-Path $workspace 'src/observed/springboot-example/target/springboot-observed-example-0.3.0.jar'
if(-not (Test-Path -LiteralPath $jar)){throw 'Build first: mvn -B -ntp verify'}
foreach($port in @(8085,8185)){
    if(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue){throw "Port $port is occupied; no unrelated process will be stopped"}
}
$changed=@{}
function Set-DeploymentValue([string]$name,[string]$value){
    $changed[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
    [Environment]::SetEnvironmentVariable($name,$value,'Process')
}
try{
    if($EnrollmentFile){
        $path=(Resolve-Path -LiteralPath $EnrollmentFile).Path
        if((Get-Item -LiteralPath $path).Length -gt 16384){throw 'Enrollment file too large'}
        $enrollment=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        Set-DeploymentValue 'AIOPS_SDK_REGISTRATION_TOKEN' $enrollment.registration_token
        Set-DeploymentValue 'AIOPS_OBSERVED_NAME' $enrollment.name
        Set-DeploymentValue 'AIOPS_SDK_SERVICE_NAME' $enrollment.service
        Set-DeploymentValue 'AIOPS_ENV' $enrollment.env
        Set-DeploymentValue 'AIOPS_SDK_REGISTRATION_URL' ($enrollment.agent_url+'/v1/observed/register')
        Set-DeploymentValue 'AIOPS_SDK_ADVERTISE_URL' $enrollment.sdk_origin
        Set-DeploymentValue 'AIOPS_BUSINESS_ADVERTISE_URL' $enrollment.business_origin
    }
    if($Drills){Set-DeploymentValue 'JAVA_OBSERVED_DRILLS' 'true'}
    Push-Location $workspace
    try{& java -jar $jar}finally{Pop-Location}
}finally{foreach($entry in $changed.GetEnumerator()){[Environment]::SetEnvironmentVariable($entry.Key,$entry.Value,'Process')}}
