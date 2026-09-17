# 从 TigerVNC 官方发布页安装固定版本的独立窗口组件，不注册服务或开放端口。
param(
    [string]$DestinationDirectory = (Join-Path $env:LOCALAPPDATA 'OpenCLI/browser-viewer/1.16.2')
)

$ErrorActionPreference = 'Stop'
$viewerFileName = 'vncviewer64-1.16.2.exe'
$viewerHash = '58396D99556026DA6B906C9ED51AD6CB5C840CC3FB65E53653C52CA5A80BFAD9'
$viewerDirectory = [System.IO.Path]::GetFullPath($DestinationDirectory)
$viewerPath = Join-Path $viewerDirectory $viewerFileName
New-Item -ItemType Directory -Path $viewerDirectory -Force | Out-Null
if (Test-Path -LiteralPath $viewerPath) {
    if ((Get-FileHash -LiteralPath $viewerPath -Algorithm SHA256).Hash -ne $viewerHash) {
        throw '目标文件已存在且校验不匹配，未覆盖。请使用新的安装目录。'
    }
    Write-Output $viewerPath
    return
}

$viewerTemporaryPath = Join-Path $viewerDirectory ([guid]::NewGuid().ToString('N') + '.download')
try {
    $releaseUrl = "https://sourceforge.net/projects/tigervnc/files/stable/1.16.2/$viewerFileName/download"
    Invoke-WebRequest -Uri $releaseUrl -OutFile $viewerTemporaryPath -TimeoutSec 90
    $downloaded = [System.IO.File]::ReadAllBytes($viewerTemporaryPath)
    if ($downloaded.Length -lt 2 -or $downloaded[0] -ne 77 -or $downloaded[1] -ne 90) {
        # 官方页面有时返回带一次性镜像链接的 HTML；只接受同一官方文件地址。
        $page = [System.Text.Encoding]::UTF8.GetString($downloaded)
        $link = [regex]::Match($page, 'https://downloads\.sourceforge\.net/[^"<> ]+')
        if (-not $link.Success) { throw '官方页面没有提供下载地址。' }
        $downloadUrl = [uri][System.Net.WebUtility]::HtmlDecode($link.Value)
        if ($downloadUrl.Scheme -ne 'https' -or $downloadUrl.Host -ne 'downloads.sourceforge.net' -or
            $downloadUrl.AbsolutePath -ne "/project/tigervnc/stable/1.16.2/$viewerFileName") {
            throw '官方下载地址校验失败。'
        }
        Invoke-WebRequest -Uri $downloadUrl -OutFile $viewerTemporaryPath -TimeoutSec 90
    }
    if ((Get-FileHash -LiteralPath $viewerTemporaryPath -Algorithm SHA256).Hash -ne $viewerHash) {
        throw '桌面组件文件校验失败，未安装。'
    }
    if ((Get-AuthenticodeSignature -LiteralPath $viewerTemporaryPath).Status -ne 'Valid') {
        throw '桌面组件签名无效，未安装。'
    }
    Move-Item -LiteralPath $viewerTemporaryPath -Destination $viewerPath
    Write-Output $viewerPath
}
finally {
    if (Test-Path -LiteralPath $viewerTemporaryPath) { Remove-Item -LiteralPath $viewerTemporaryPath }
}
