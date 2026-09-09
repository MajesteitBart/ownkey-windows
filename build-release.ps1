[CmdletBinding(DefaultParameterSetName = 'Thumbprint')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Thumbprint')]
    [string]$CertificateThumbprint,

    [Parameter(Mandatory = $true, ParameterSetName = 'Subject')]
    [string]$CertificateSubject,

    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$CertificateStoreLocation = 'CurrentUser',

    [string]$TimestampUrl = 'http://timestamp.digicert.com',

    [string]$SignToolPath,

    [string]$IsccPath,

    [switch]$SkipDependencyInstall,

    [switch]$Force
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$builder = Join-Path $PSScriptRoot 'scripts\Build-WindowsInstaller.ps1'
& $builder -Mode PublicRelease @PSBoundParameters
