# Деплой стека на удалённый Linux-хост через SSH/SCP. Требует модуль Posh-SSH.
# Аутентификация — интерактивный запрос (пароль НЕ хранится в файле).
# Рекомендуется заранее настроить вход по SSH-ключу.

Import-Module Posh-SSH

$Server    = "<SERVER_IP>"
$User      = "root"
$RemoteDir = "/opt/lead-intake"

# Безопасный запрос учётных данных вместо пароля в коде.
$cred = Get-Credential -UserName $User -Message "SSH-доступ к $Server"

# -AcceptKey = trust-on-first-use: ключ хоста запоминается при первом коннекте.
# Для прод-окружения добавьте ключ хоста в known_hosts заранее и уберите -AcceptKey.
$session = New-SSHSession -ComputerName $Server -Credential $cred -AcceptKey

try {
    Invoke-SSHCommand -SSHSession $session -Command "mkdir -p $RemoteDir" | Out-Null
    Set-SCPItem -SSHSession $session -Path "./*" -Destination $RemoteDir -Recurse

    # .env не перезаписываем (-n). ВНИМАНИЕ: на сервере пропишите реальные значения в .env!
    (Invoke-SSHCommand -SSHSession $session -Command "cd $RemoteDir && cp -n .env.example .env && docker compose up -d --build").Output
    (Invoke-SSHCommand -SSHSession $session -Command "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'").Output
}
finally {
    Remove-SSHSession -SSHSession $session | Out-Null
}
