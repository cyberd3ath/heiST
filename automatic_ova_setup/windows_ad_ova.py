import sys
import time
import socket
import shutil
import random
import subprocess
from pathlib import Path

DOMAIN_NAME = "corp.local"
DOMAIN_NETBIOS = "CORP"
DOMAIN_SAFE_MODE_PASSWORD = "P@ssw0rd123!"

ADMIN_USERNAME = "ctf_admin"
VM_PASSWORD = "Admin123"

VM_MEMORY = 4096
VM_CPUS = 2
VM_DISK_SIZE_MB = 32 * 1024

WORKING_DIR = Path(__file__).resolve().parent / "vm-build-ad"
ISO_DIR = WORKING_DIR / "iso"
OVA_OUTPUT_DIR = WORKING_DIR / "ova"

WINDOWS_ISO_URL = "https://go.microsoft.com/fwlink/p/?LinkID=2195174&clcid=0x409&culture=en-us&country=US"
VIRTIO_ISO_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.285-1/virtio-win-0.1.285.iso"
WINDOWS_ISO_NAME = "windows_server_2016.iso"
VIRTIO_ISO_NAME = "virtio-win-0.1.285.iso"
WINDOWS_ISO = ISO_DIR / WINDOWS_ISO_NAME
VIRTIO_ISO = ISO_DIR / VIRTIO_ISO_NAME

VBOXMANAGE = shutil.which("VBoxManage") or shutil.which("vboxmanage") or "VBoxManage"

NATNETWORK_NAME = "ctf-ad-natnet"
NATNETWORK_CIDR = "10.50.0.0/24"
NATNETWORK_PREFIX = "10.50.0."

VMS = {
    "dc": {
        "VM_ID": 210,
        "VM_NAME": "dc01-corp",
        "COMPUTER_NAME": "DC01",
        "ROLE": "dc",
        "SSH_HOST_PORT": random.randint(20000, 30000),
    },
    "member": {
        "VM_ID": 211,
        "VM_NAME": "srv01-corp",
        "COMPUTER_NAME": "SRV01",
        "ROLE": "member",
        "SSH_HOST_PORT": random.randint(30001, 40000),
    },
}


def run_command(cmd, check=True, wait_time=0, capture=False):
    """Run a shell command, printing it first."""
    print(f"$ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if wait_time:
        time.sleep(wait_time)
    if check and result.returncode != 0:
        stderr = result.stderr if capture else "(see output above)"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(str(c) for c in cmd)}\n{stderr}")
    return result


def download_file(url, dest: Path, label: str):
    if dest.exists():
        print(f"{label} already downloaded: {dest}")
        return
    print(f"Downloading {label} -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    run_command(["curl", "-L", "--fail", "-o", str(tmp), url])
    tmp.rename(dest)
    print(f"Downloaded {label}")


def create_iso(src_dir: Path, iso_path: Path):
    """Build a small ISO (autounattend + setup.ps1) using genisoimage/xorriso."""
    tool = shutil.which("genisoimage") or shutil.which("mkisofs") or shutil.which("xorriso")
    if tool is None:
        raise RuntimeError(
            "Need genisoimage, mkisofs, or xorriso installed on the host "
            "(e.g. `apt install genisoimage`) to build the autounattend ISO."
        )
    if "xorriso" in tool:
        cmd = [tool, "-as", "genisoimage", "-J", "-R", "-o", str(iso_path), str(src_dir)]
    else:
        cmd = [tool, "-J", "-R", "-o", str(iso_path), str(src_dir)]
    run_command(cmd)


def generate_setup_script(vm_dir: Path):
    """First-boot script: network check, TLS 1.2, OpenSSH install. DC promotion/domain join happen later over SSH, not here."""
    script_content = """$LogFile = "C:\\Windows\\Temp\\setup.log"
$ErrorActionPreference = "Continue"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] [$Level] $Message"
    Write-Host $logMessage
    Add-Content -Path $LogFile -Value $logMessage
}

Write-Log "Windows Server 2016 Setup START" "INFO"

Start-Sleep -Seconds 10

Write-Log "Testing network connectivity..." "INFO"
$retries = 0
$maxRetries = 10
$connected = $false

while ($retries -lt $maxRetries) {
    try {
        $testConnection = Test-NetConnection -ComputerName github.com -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($testConnection) {
            Write-Log "Network connectivity confirmed" "INFO"
            $connected = $true
            break
        }
    } catch {
        Write-Log "Connection test failed: $_" "WARN"
    }

    $retries++
    Write-Log "Network not ready, retry $retries/$maxRetries" "WARN"
    Start-Sleep -Seconds 5
}

if (-not $connected) {
    Write-Log "Failed to establish network connectivity after $maxRetries attempts" "ERROR"
}

Write-Log "Enabling TLS 1.2" "INFO"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Log "TLS 1.2 enabled" "INFO"
} catch {
    Write-Log "Failed to enable TLS 1.2: $_" "ERROR"
}

Write-Log "Creating temp directory" "INFO"
try {
    $tempPath = "C:\\temp"
    if (-not (Test-Path $tempPath)) {
        New-Item -Path $tempPath -ItemType Directory -Force | Out-Null
    }
    Write-Log "Created C:\\temp" "INFO"
} catch {
    Write-Log "Failed to create C:\\temp: $_" "ERROR"
}

Write-Log "Installing OpenSSH" "INFO"
try {
    Set-Location "C:\\temp"

    Write-Log "Downloading OpenSSH" "INFO"
    $opensshUrl = "https://github.com/PowerShell/Win32-OpenSSH/releases/download/v9.5.0.0p1-Beta/OpenSSH-Win64.zip"
    $opensshZip = "C:\\temp\\OpenSSH-Win64.zip"
    Invoke-WebRequest -Uri $opensshUrl -OutFile $opensshZip -UseBasicParsing
    Write-Log "OpenSSH downloaded" "INFO"

    Write-Log "Extracting OpenSSH" "INFO"
    $opensshDest = "C:\\Program Files\\OpenSSH"
    if (Get-Command Expand-Archive -ErrorAction SilentlyContinue) {
        Expand-Archive -Path $opensshZip -DestinationPath $opensshDest -Force
    } else {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        if (Test-Path $opensshDest) {
            Remove-Item $opensshDest -Recurse -Force
        }
        [System.IO.Compression.ZipFile]::ExtractToDirectory(
            $opensshZip,
            $opensshDest
        )
    }
    Write-Log "OpenSSH extracted" "INFO"

    Write-Log "Installing OpenSSH service" "INFO"
    $opensshPath = "C:\\Program Files\\OpenSSH\\OpenSSH-Win64"
    Set-Location $opensshPath
    & ".\\install-sshd.ps1"
    Write-Log "OpenSSH service installed" "INFO"

    Write-Log "Starting OpenSSH service" "INFO"
    Set-Service -Name sshd -StartupType Automatic
    Start-Service sshd
    Write-Log "OpenSSH service started" "INFO"

    Write-Log "Configuring firewall for SSH" "INFO"
    New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction SilentlyContinue
    Write-Log "Firewall rule created" "INFO"

} catch {
    Write-Log "OpenSSH installation failed: $_" "ERROR"
    Write-Log "Exception: $($_.Exception.Message)" "ERROR"
}

Write-Log "Configuring TLS 1.2 in registry" "INFO"
try {
    reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecurityProviders\\SCHANNEL\\Protocols\\TLS 1.2\\Server" /v Enabled /t REG_DWORD /d 1 /f | Out-Null
    reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecurityProviders\\SCHANNEL\\Protocols\\TLS 1.2\\Server" /v DisabledByDefault /t REG_DWORD /d 0 /f | Out-Null
    reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecurityProviders\\SCHANNEL\\Protocols\\TLS 1.2\\Client" /v Enabled /t REG_DWORD /d 1 /f | Out-Null
    reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecurityProviders\\SCHANNEL\\Protocols\\TLS 1.2\\Client" /v DisabledByDefault /t REG_DWORD /d 0 /f | Out-Null
    reg add "HKLM\\SOFTWARE\\Microsoft\\.NETFramework\\v4.0.30319" /v SchUseStrongCrypto /t REG_DWORD /d 1 /f | Out-Null
    reg add "HKLM\\SOFTWARE\\Wow6432Node\\Microsoft\\.NETFramework\\v4.0.30319" /v SchUseStrongCrypto /t REG_DWORD /d 1 /f | Out-Null
    Write-Log "TLS 1.2 registry configured" "INFO"
} catch {
    Write-Log "Failed to configure TLS 1.2 registry: $_" "ERROR"
}

Write-Log "Configuring PowerShell alias" "INFO"
try {
    reg add "HKCU\\Software\\Microsoft\\Command Processor" /v AutoRun /t REG_SZ /d "doskey ps=powershell `$*" /f | Out-Null
    reg add "HKLM\\Software\\Microsoft\\Command Processor" /v AutoRun /t REG_SZ /d "doskey ps=powershell `$*" /f | Out-Null
    Write-Log "PowerShell alias configured" "INFO"
} catch {
    Write-Log "PowerShell alias configuration failed" "ERROR"
}

Write-Log "SETUP COMPLETE" "INFO"
Write-Log "Full log available at: $LogFile" "INFO"
"""

    setup_script_path = vm_dir / "setup.ps1"
    with open(setup_script_path, 'w', encoding='utf-8') as f:
        f.write(script_content)

    print(f"Generated setup script: {setup_script_path}")
    return setup_script_path


def generate_autounattend_xml(vm_dir: Path, computer_name: str):
    """Generate Windows unattended installation configuration file."""
    xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
    <settings pass="windowsPE">
        <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <SetupUILanguage>
                <UILanguage>en-US</UILanguage>
            </SetupUILanguage>
            <InputLocale>0407:00000407</InputLocale>
            <SystemLocale>en-US</SystemLocale>
            <UILanguage>en-US</UILanguage>
            <UILanguageFallback>en-US</UILanguageFallback>
            <UserLocale>en-US</UserLocale>
        </component>
	    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <DiskConfiguration>
                <Disk wcm:action="add">
                    <CreatePartitions>
                        <CreatePartition wcm:action="add">
                            <Order>1</Order>
                            <Size>500</Size>
                            <Type>Primary</Type>
                        </CreatePartition>
                        <CreatePartition wcm:action="add">
                            <Order>2</Order>
                            <Extend>true</Extend>
                            <Type>Primary</Type>
                        </CreatePartition>
                    </CreatePartitions>
                    <ModifyPartitions>
                        <ModifyPartition wcm:action="add">
                            <Order>1</Order>
                            <PartitionID>1</PartitionID>
                            <Active>true</Active>
                            <Format>NTFS</Format>
                            <Label>System Reserved</Label>
                        </ModifyPartition>
                        <ModifyPartition wcm:action="add">
                            <Order>2</Order>
                            <PartitionID>2</PartitionID>
                            <Format>NTFS</Format>
                            <Label>Windows</Label>
                        </ModifyPartition>
                    </ModifyPartitions>
                    <DiskID>0</DiskID>
                    <WillWipeDisk>true</WillWipeDisk>
                </Disk>
            </DiskConfiguration>
            <ImageInstall>
                <OSImage>
                    <InstallFrom>
                        <MetaData wcm:action="add">
                            <Key>/IMAGE/NAME</Key>
                            <Value>Windows Server 2016 SERVERSTANDARDCORE</Value>
                        </MetaData>
                    </InstallFrom>
                    <InstallTo>
                        <DiskID>0</DiskID>
                        <PartitionID>2</PartitionID>
                    </InstallTo>
                    <WillShowUI>OnError</WillShowUI>
                </OSImage>
            </ImageInstall>
            <UserData>
                <AcceptEula>true</AcceptEula>
            </UserData>
        </component>
        <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <DriverPaths>
                <PathAndCredentials wcm:action="add" wcm:keyValue="1">
                    <Path>D:\\viostor\\2k16\\amd64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="2">
                    <Path>D:\\NetKVM\\2k16\\amd64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="3">
                    <Path>D:\\vioscsi\\2k16\\amd64</Path>
                </PathAndCredentials>
                <PathAndCredentials wcm:action="add" wcm:keyValue="4">
                    <Path>D:\\amd64\\2k16</Path>
                </PathAndCredentials>
            </DriverPaths>
        </component>
    </settings>
    <settings pass="oobeSystem">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <AutoLogon>
                <Password>
                    <Value>QQBkAG0AaQBuADEAMgAzAFAAYQBzAHMAdwBvAHIAZAA=</Value>
                    <PlainText>false</PlainText>
                </Password>
                <Enabled>true</Enabled>
                <Username>{ADMIN_USERNAME}</Username>
            </AutoLogon>
            <UserAccounts>
                <AdministratorPassword>
                    <Value>QQBkAG0AaQBuADEAMgAzAEEAZABtAGkAbgBpAHMAdAByAGEAdABvAHIAUABhAHMAcwB3AG8AcgBkAA==</Value>
                    <PlainText>false</PlainText>
                </AdministratorPassword>
                <LocalAccounts>
                    <LocalAccount wcm:action="add">
                        <Password>
                            <Value>QQBkAG0AaQBuADEAMgAzAFAAYQBzAHMAdwBvAHIAZAA=</Value>
                            <PlainText>false</PlainText>
                        </Password>
                        <Group>Administrators</Group>
                        <Name>{ADMIN_USERNAME}</Name>
                    </LocalAccount>
                </LocalAccounts>
            </UserAccounts>
            <OOBE>
                <HideEULAPage>true</HideEULAPage>
                <HideLocalAccountScreen>true</HideLocalAccountScreen>
                <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
                <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
                <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
                <ProtectYourPC>3</ProtectYourPC>
            </OOBE>
            <FirstLogonCommands>
                <SynchronousCommand wcm:action="add">
                    <Order>1</Order>
                    <CommandLine>cmd.exe /c for %D in (D E F G) do if exist %D:\\virtio-win-guest-tools.exe %D:\\virtio-win-guest-tools.exe /quiet /norestart</CommandLine>
                    <Description>Install VirtIO Guest Tools (drivers + qemu-ga)</Description>
                    <RequiresUserInput>false</RequiresUserInput>
                </SynchronousCommand>
                <SynchronousCommand wcm:action="add">
                    <Order>2</Order>
                    <CommandLine>powershell.exe -ExecutionPolicy Bypass -Command &quot;Start-Sleep -Seconds 15&quot;</CommandLine>
                    <Description>Wait for VirtIO tools</Description>
                    <RequiresUserInput>false</RequiresUserInput>
                </SynchronousCommand>
                <SynchronousCommand wcm:action="add">
                    <Order>3</Order>
                    <CommandLine>cmd.exe /c for %D in (D E F G) do if exist %D:\\setup.ps1 powershell.exe -ExecutionPolicy Bypass -File %D:\\setup.ps1</CommandLine>
                    <Description>Run setup script</Description>
                    <RequiresUserInput>false</RequiresUserInput>
                </SynchronousCommand>
            </FirstLogonCommands>
        </component>
    </settings>
    <settings pass="specialize">
        <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <ComputerName>{computer_name}</ComputerName>
        </component>
    </settings>
</unattend>
"""

    autounattend_path = vm_dir / "autounattend.xml"
    with open(autounattend_path, 'w', encoding='utf-8') as f:
        f.write(xml_content)

    print(f"Generated autounattend.xml: {autounattend_path}")
    return autounattend_path


def create_autounattend_iso(vm_dir: Path, iso_path: Path, autounattend_path: Path, setup_script_path: Path):
    """Create bootable-data ISO with autounattend.xml and setup script."""
    if iso_path.exists():
        iso_path.unlink()

    temp_dir = vm_dir / "iso_temp"
    temp_dir.mkdir(exist_ok=True)

    shutil.copy(autounattend_path, temp_dir / "autounattend.xml")
    shutil.copy(setup_script_path, temp_dir / "setup.ps1")

    try:
        create_iso(temp_dir, iso_path)
        print(f"Created autounattend ISO: {iso_path}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def vbox_vm_exists(name):
    result = run_command([VBOXMANAGE, "list", "vms"], capture=True, check=False)
    return f'"{name}"' in result.stdout


def vbox_vm_state(name):
    result = run_command([VBOXMANAGE, "showvminfo", name, "--machinereadable"], capture=True, check=False)
    for line in result.stdout.splitlines():
        if line.startswith("VMState="):
            return line.split("=", 1)[1].strip('"')
    return "unknown"


def vbox_delete_vm(name):
    print(f"VM {name} already exists. Deleting...")
    run_command([VBOXMANAGE, "controlvm", name, "poweroff"], check=False)
    time.sleep(3)
    run_command([VBOXMANAGE, "unregistervm", name, "--delete"], check=False)


def ensure_nat_network():
    """Create the shared NAT Network used for DC<->member traffic if it doesn't exist yet."""
    result = run_command([VBOXMANAGE, "list", "natnets"], capture=True, check=False)
    if f"NetworkName:    {NATNETWORK_NAME}" in result.stdout or NATNETWORK_NAME in result.stdout:
        print(f"NAT Network {NATNETWORK_NAME} already exists")
        return

    print(f"Creating NAT Network {NATNETWORK_NAME} ({NATNETWORK_CIDR})...")
    run_command([
        VBOXMANAGE, "natnetwork", "add",
        "--netname", NATNETWORK_NAME,
        "--network", NATNETWORK_CIDR,
        "--enable",
        "--dhcp", "on",
    ])


def create_virtualbox_vm(vm_config, vm_dir: Path, windows_iso: Path, virtio_iso: Path, autounattend_iso: Path):
    """Create and configure the VirtualBox VM used to run the install."""
    name = vm_config["VM_NAME"]
    print(f"Creating VirtualBox VM {name}...")

    if vbox_vm_exists(name):
        vbox_delete_vm(name)

    vm_dir.mkdir(parents=True, exist_ok=True)
    disk_path = vm_dir / f"{name}.vdi"

    run_command([
        VBOXMANAGE, "createvm",
        "--name", name,
        "--ostype", "Windows2016_64",
        "--basefolder", str(WORKING_DIR),
        "--register",
    ])

    run_command([
        VBOXMANAGE, "modifyvm", name,
        "--memory", str(VM_MEMORY),
        "--cpus", str(VM_CPUS),
        "--nic1", "nat",
        "--nic2", "natnetwork",
        "--nat-network2", NATNETWORK_NAME,
        "--graphicscontroller", "vboxsvga",
        "--audio-driver", "none",
        "--boot1", "dvd",
        "--boot2", "disk",
        "--boot3", "none",
        "--boot4", "none",
    ])

    run_command([
        VBOXMANAGE, "modifyvm", name,
        "--natpf1", f"guestssh,tcp,,{vm_config['SSH_HOST_PORT']},,22",
    ])

    run_command([
        VBOXMANAGE, "createmedium", "disk",
        "--filename", str(disk_path),
        "--size", str(VM_DISK_SIZE_MB),
        "--format", "VDI",
    ])

    run_command([
        VBOXMANAGE, "storagectl", name,
        "--name", "SATA Controller",
        "--add", "sata",
        "--controller", "IntelAhci",
        "--portcount", "4",
        "--bootable", "on",
    ])

    run_command([
        VBOXMANAGE, "storageattach", name,
        "--storagectl", "SATA Controller",
        "--port", "0", "--device", "0",
        "--type", "hdd",
        "--medium", str(disk_path),
    ])

    run_command([
        VBOXMANAGE, "storageattach", name,
        "--storagectl", "SATA Controller",
        "--port", "1", "--device", "0",
        "--type", "dvddrive",
        "--medium", str(windows_iso),
    ])

    run_command([
        VBOXMANAGE, "storageattach", name,
        "--storagectl", "SATA Controller",
        "--port", "2", "--device", "0",
        "--type", "dvddrive",
        "--medium", str(virtio_iso),
    ])

    run_command([
        VBOXMANAGE, "storageattach", name,
        "--storagectl", "SATA Controller",
        "--port", "3", "--device", "0",
        "--type", "dvddrive",
        "--medium", str(autounattend_iso),
    ])

    print(f"VM {name} configuration complete")


def start_vm_headless(name):
    print(f"Starting VM {name} (headless)...")
    run_command([VBOXMANAGE, "startvm", name, "--type", "headless"])


def wait_for_ssh(port, host="127.0.0.1", timeout=3600, interval=10):
    print(f"Waiting for SSH on {host}:{port} (timeout {timeout}s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                print("SSH port is open")
                return True
        except OSError:
            time.sleep(interval)
    return False


def _get_paramiko():
    try:
        import paramiko
        return paramiko
    except ImportError:
        raise RuntimeError(
            "paramiko is required to build the AD environment (unlike the single-VM "
            "OVA script, DC promotion and domain join need real SSH-driven orchestration, "
            "not just a completion marker). Install it with `pip install paramiko`."
        )


def ssh_connect(port, host="127.0.0.1", username=ADMIN_USERNAME, password=VM_PASSWORD, timeout=15):
    paramiko = _get_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=timeout)
    return client


def ssh_run(port, command, timeout=120, username=ADMIN_USERNAME, password=VM_PASSWORD):
    """Run a PowerShell command over SSH and return (exit_status, stdout, stderr)."""
    client = ssh_connect(port, username=username, password=password)
    try:
        full_cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{command}"'
        _, stdout, stderr = client.exec_command(full_cmd, timeout=timeout)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore")
        return exit_status, out, err
    finally:
        client.close()


def ssh_run_expect_disconnect(port, command, username=ADMIN_USERNAME, password=VM_PASSWORD):
    """Run a command expected to trigger a reboot; a dropped connection here is expected, caller polls for the box to come back up."""
    try:
        client = ssh_connect(port, username=username, password=password)
        full_cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{command}"'
        client.exec_command(full_cmd, timeout=60)
        time.sleep(5)
        client.close()
    except Exception as e:
        print(f"[Info] SSH session dropped while triggering reboot (expected): {e}")


def sftp_put(port, local_path: Path, remote_path: str, username=ADMIN_USERNAME, password=VM_PASSWORD):
    client = ssh_connect(port, username=username, password=password)
    try:
        sftp = client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
        finally:
            sftp.close()
    finally:
        client.close()


def wait_for_setup_complete(port, timeout=1800, interval=15):
    """Poll the guest over SSH for the 'SETUP COMPLETE' marker in setup.log."""
    print("Polling setup.log over SSH for completion marker...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            exit_status, out, _ = ssh_run(
                port,
                'Get-Content C:\\Windows\\Temp\\setup.log -ErrorAction SilentlyContinue',
                timeout=20,
            )
            if "SETUP COMPLETE" in out:
                print("Setup script reported completion")
                return True
        except Exception as e:
            print(f"SSH check not ready yet: {e}")
        time.sleep(interval)

    print("WARNING: timed out waiting for SETUP COMPLETE marker")
    return False


def get_internal_ip(port, timeout=120, interval=5):
    """SSH in and ask the guest for its DHCP-assigned IPv4 on nic2, rather than guessing from the host side."""
    print("Discovering internal (nic2) IP address via SSH...")
    deadline = time.time() + timeout
    cmd = (
        f"(Get-NetIPAddress -AddressFamily IPv4 | "
        f"Where-Object {{ $_.IPAddress -like '{NATNETWORK_PREFIX}*' }} | "
        f"Select-Object -First 1 -ExpandProperty IPAddress)"
    )
    while time.time() < deadline:
        try:
            exit_status, out, _ = ssh_run(port, cmd, timeout=20)
            ip = out.strip()
            if ip.startswith(NATNETWORK_PREFIX):
                print(f"Discovered internal IP: {ip}")
                return ip
        except Exception as e:
            print(f"IP discovery not ready yet: {e}")
        time.sleep(interval)

    raise TimeoutError("Timed out discovering internal IP address")


def promote_dc(port, timeout_reboot=1800):
    """Install AD DS and promote the box to a Domain Controller for corp.local."""
    print("Promoting DC: installing AD-Domain-Services feature...")
    exit_status, out, err = ssh_run(
        port,
        "Install-WindowsFeature -Name AD-Domain-Services -IncludeManagementTools | Out-String",
        timeout=300,
    )
    print(out)
    if exit_status != 0:
        raise RuntimeError(f"Failed to install AD-Domain-Services: {err}")

    print("Promoting DC: running Install-ADDSForest (this will reboot the VM)...")
    promote_cmd = (
        f'$p = ConvertTo-SecureString "{DOMAIN_SAFE_MODE_PASSWORD}" -AsPlainText -Force; '
        f'Install-ADDSForest -DomainName "{DOMAIN_NAME}" -DomainNetbiosName "{DOMAIN_NETBIOS}" '
        f'-SafeModeAdministratorPassword $p -InstallDns -Force -NoRebootOnCompletion:$false'
    )
    ssh_run_expect_disconnect(port, promote_cmd)

    print("Waiting for DC to come back up after promotion reboot...")
    if not wait_for_ssh(port, timeout=timeout_reboot):
        raise RuntimeError("DC did not come back up after promotion reboot")

    print("Verifying AD DS is actually running...")
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            exit_status, out, _ = ssh_run(
                port,
                "(Get-Service NTDS -ErrorAction SilentlyContinue).Status",
                timeout=20,
            )
            if out.strip() == "Running":
                print("Domain Controller promotion confirmed (NTDS service running)")
                return
        except Exception as e:
            print(f"DC verification not ready yet: {e}")
        time.sleep(15)

    raise RuntimeError("Timed out waiting for NTDS service to come up after DC promotion")


def join_domain(port, dc_internal_ip, timeout_reboot=1800):
    """Point DNS at the DC just long enough to join the domain, then reset to automatic (DHCP) so nothing static survives into the OVA."""
    print(f"Pointing member's internal NIC DNS at DC ({dc_internal_ip}) for domain join...")
    dns_cmd = (
        f"$nic = Get-NetAdapter | Where-Object {{ (Get-NetIPAddress -InterfaceIndex $_.ifIndex "
        f"-AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress -like '{NATNETWORK_PREFIX}*' }} "
        f"| Select-Object -First 1; "
        f"Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ServerAddresses {dc_internal_ip}"
    )
    exit_status, out, err = ssh_run(port, dns_cmd, timeout=60)
    if exit_status != 0:
        raise RuntimeError(f"Failed to set temporary DNS for domain join: {err}")

    print(f"Joining domain {DOMAIN_NAME} (this will reboot the VM)...")
    join_cmd = (
        f'$u = "{DOMAIN_NETBIOS}\\Administrator"; '
        f'$p = ConvertTo-SecureString "{VM_PASSWORD}" -AsPlainText -Force; '
        f'$cred = New-Object System.Management.Automation.PSCredential($u, $p); '
        f'Add-Computer -DomainName "{DOMAIN_NAME}" -Credential $cred -Force -Restart'
    )
    ssh_run_expect_disconnect(port, join_cmd)

    print("Waiting for member to come back up after domain-join reboot...")
    if not wait_for_ssh(port, timeout=timeout_reboot):
        raise RuntimeError("Member did not come back up after domain-join reboot")

    print("Verifying domain join...")
    deadline = time.time() + 300
    joined = False
    while time.time() < deadline:
        try:
            exit_status, out, _ = ssh_run(
                port,
                "(Get-WmiObject Win32_ComputerSystem).PartOfDomain",
                timeout=20,
            )
            if out.strip().lower() == "true":
                joined = True
                break
        except Exception as e:
            print(f"Domain-join verification not ready yet: {e}")
        time.sleep(15)

    if not joined:
        raise RuntimeError("Member did not report PartOfDomain=True after join")

    print("Domain join confirmed. Resetting internal NIC DNS back to automatic (DHCP)...")
    reset_cmd = (
        f"$nic = Get-NetAdapter | Where-Object {{ (Get-NetIPAddress -InterfaceIndex $_.ifIndex "
        f"-AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress -like '{NATNETWORK_PREFIX}*' }} "
        f"| Select-Object -First 1; "
        f"Set-DnsClientServerAddress -InterfaceIndex $nic.ifIndex -ResetServerAddresses"
    )
    exit_status, out, err = ssh_run(port, reset_cmd, timeout=60)
    if exit_status != 0:
        print(f"[Warning] Failed to reset DNS back to automatic: {err}")


def shutdown_vm(name, timeout=300):
    print(f"Shutting down VM {name} (ACPI power button)...")
    run_command([VBOXMANAGE, "controlvm", name, "acpipowerbutton"], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vbox_vm_state(name) == "poweroff":
            print("VM powered off")
            return
        time.sleep(5)
    print("VM didn't shut down gracefully in time, forcing power off...")
    run_command([VBOXMANAGE, "controlvm", name, "poweroff"], check=False)
    time.sleep(5)


def detach_isos(name):
    """Detach the DVDs before export so the OVA doesn't carry the install ISOs."""
    for port in ("1", "2", "3"):
        run_command([
            VBOXMANAGE, "storageattach", name,
            "--storagectl", "SATA Controller",
            "--port", port, "--device", "0",
            "--type", "dvddrive",
            "--medium", "none",
        ], check=False)


def export_ova(name, output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    print(f"Exporting {name} -> {output_path}")
    run_command([
        VBOXMANAGE, "export", name,
        "--output", str(output_path),
        "--ovf10",
        "--options", "nomacs",
    ])
    print(f"Export complete: {output_path}")


def build_and_boot_vm(vm_config):
    """Download ISOs (once), create the VM, boot it, and wait for first-boot setup to finish."""
    name = vm_config["VM_NAME"]
    vm_dir = WORKING_DIR / name
    port = vm_config["SSH_HOST_PORT"]

    vm_dir.mkdir(parents=True, exist_ok=True)
    autounattend_iso = ISO_DIR / f"autounattend-{vm_config['VM_ID']}.iso"

    setup_script_path = generate_setup_script(vm_dir)
    autounattend_path = generate_autounattend_xml(vm_dir, vm_config["COMPUTER_NAME"])
    create_autounattend_iso(vm_dir, autounattend_iso, autounattend_path, setup_script_path)

    create_virtualbox_vm(vm_config, vm_dir, WINDOWS_ISO, VIRTIO_ISO, autounattend_iso)
    start_vm_headless(name)

    if not wait_for_ssh(port):
        print(f"ERROR: SSH never came up for {name} - check the install (e.g. VBoxManage --type gui, "
              f"or screenshot via VBoxManage controlvm {name} screenshotpng).")
        sys.exit(1)

    wait_for_setup_complete(port)


def main():
    if shutil.which(VBOXMANAGE) is None:
        print("ERROR: VBoxManage not found. Install VirtualBox on this host first.")
        sys.exit(1)

    _get_paramiko()

    print("=" * 60)
    print("Active Directory Domain Environment - Automated Setup (VirtualBox -> OVA)")
    print("=" * 60)
    print(f"Domain: {DOMAIN_NAME} ({DOMAIN_NETBIOS})")
    for vm_config in VMS.values():
        print(f"  - {vm_config['VM_NAME']} (role={vm_config['ROLE']}, "
              f"SSH forwarded port: {vm_config['SSH_HOST_PORT']})")
    print("=" * 60)

    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    ISO_DIR.mkdir(parents=True, exist_ok=True)

    download_file(WINDOWS_ISO_URL, WINDOWS_ISO, "Windows Server 2016 ISO")
    download_file(VIRTIO_ISO_URL, VIRTIO_ISO, "VirtIO drivers ISO")

    ensure_nat_network()

    dc_config = VMS["dc"]
    member_config = VMS["member"]

    print("\n--- Building Domain Controller ---")
    build_and_boot_vm(dc_config)
    promote_dc(dc_config["SSH_HOST_PORT"])
    dc_internal_ip = get_internal_ip(dc_config["SSH_HOST_PORT"])

    print("\n--- Building Domain Member ---")
    build_and_boot_vm(member_config)
    join_domain(member_config["SSH_HOST_PORT"], dc_internal_ip)

    print("\n--- Finalizing (shutdown, detach ISOs, export OVAs) ---")
    for vm_config in VMS.values():
        name = vm_config["VM_NAME"]
        ova_path = OVA_OUTPUT_DIR / f"{name}.ova"
        shutdown_vm(name)
        detach_isos(name)
        export_ova(name, ova_path)

    print("\n" + "=" * 60)
    print("Setup Complete")
    print("=" * 60)
    print(f"Domain: {DOMAIN_NAME}")
    print(f"Domain Admin: {DOMAIN_NETBIOS}\\Administrator / {VM_PASSWORD}")
    print(f"Local Admin: Administrator / {ADMIN_USERNAME} - both / {VM_PASSWORD}")
    for vm_config in VMS.values():
        name = vm_config["VM_NAME"]
        print(f"  {name} OVA: {OVA_OUTPUT_DIR / f'{name}.ova'}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)