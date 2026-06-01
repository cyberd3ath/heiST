# DUMMY - test script only, does not install Wazuh
param(
    [switch]$install,
    [switch]$register,
    [string]$manager = "",
    [string]$name = "",
    [string]$password = "",
    [switch]$yes
)

$installFlag = "C:\Windows\Temp\wazuh-setup-complete.flag"
$registerFlag = "C:\Windows\Temp\wazuh-register-complete.flag"
$logFile     = "C:\Windows\Temp\setup_wazuh.log"

function Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$ts $msg" | Tee-Object -FilePath $logFile -Append
}

if ($install) {
    Log "[install] Starting install phase"
    Log "[install] Creating completion flag at $installFlag"
    New-Item -ItemType File -Force -Path $installFlag | Out-Null
    Log "[install] Done"
    exit 0
}

if ($register) {
    Log "[register] Starting register phase"
    Log "[register] manager=$manager name=$name password=<redacted>"

    if (-not $manager) { Log "[register] ERROR: --manager is required"; exit 1 }
    if (-not $name)    { Log "[register] ERROR: --name is required";    exit 1 }
    if (-not $password){ Log "[register] ERROR: --password is required"; exit 1 }

    Log "[register] Creating register flag at $registerFlag"
    New-Item -ItemType File -Force -Path $registerFlag | Out-Null
    Log "[register] Done"
    exit 0
}

Log "ERROR: No phase specified (--install or --register)"
exit 1