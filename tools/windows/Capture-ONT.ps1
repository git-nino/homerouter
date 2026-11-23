#requires -Version 5.1
<#
.SYNOPSIS
    Windows-native, offline HAR analysis for an ONT/router UI.

.DESCRIPTION
    Reads a HAR exported from Edge/Chrome DevTools, keeps only requests to one
    explicitly selected host, and produces a sanitized Homerrouter draft plus
    evidence. No network probing, scanning, credentials, Python, or admin rights
    are used. The raw HAR is never copied into outputs.

.EXAMPLE
    .\Capture-ONT.bat -HarPath .\capture.har -Gateway 192.168.1.1 -OutputDirectory .\output
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $HarPath,

    # Optional manual override. This is a filter target, not an assumption that
    # this IP is the ONT. May be an IP, host, host:port, or URL.
    [string] $Gateway,

    # Optional separate UI host (for example router.local). Overrides the host
    # portion of -Gateway for filtering, but not its port.
    [string] $Hostname,

    # Optional manual override. Only used in the permanent connector id when
    # firmware differences require separate profiles for the same model.
    [string] $FirmwareFamily,

    [string] $OutputDirectory
)

$script:ToolVersion = '1.0.0'
$script:RedactionCounts = @{}
$script:SensitiveLiterals = [System.Collections.Generic.HashSet[string]]::new()

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path (Get-Location).Path 'output'
}

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
function Register-Redaction {
    param([string] $Placeholder, [int] $Count = 1)
    if ($Count -le 0 -or [string]::IsNullOrWhiteSpace($Placeholder)) { return }
    $key = $Placeholder -replace '[<>]', ''
    if (-not $script:RedactionCounts.ContainsKey($key)) { $script:RedactionCounts[$key] = 0 }
    $script:RedactionCounts[$key] += $Count
}

function Register-SensitiveLiteral {
    param([string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -lt 3) { return }
    if ($Value -like '<redacted-*') { return }
    [void] $script:SensitiveLiterals.Add($Value)
}

function Get-Prop {
    param($Object, [string] $Name)
    if ($null -eq $Object -or [string]::IsNullOrEmpty($Name)) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return ,$Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return ,$property.Value }
    return $null
}

function Get-PropNames {
    param($Object)
    if ($null -eq $Object) { return @() }
    if ($Object -is [System.Collections.IDictionary]) {
        return @($Object.Keys | ForEach-Object { [string] $_ })
    }
    return @($Object.PSObject.Properties | ForEach-Object { [string] $_.Name })
}

function To-Array {
    param($Value)
    if ($null -eq $Value) { return @() }
    return @($Value)
}

function Get-StableHash {
    param([string] $Text, [int] $Length = 10)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($bytes) } finally { $sha.Dispose() }
    $hex = ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
    if ($Length -gt $hex.Length) { $Length = $hex.Length }
    return $hex.Substring(0, $Length)
}

function New-Slug {
    param([string] $Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return 'item' }
    $slug = ([string] $Text).ToLowerInvariant() -replace '[^a-z0-9]+', '_'
    $slug = $slug.Trim('_')
    if ($slug.Length -gt 48) { $slug = $slug.Substring(0, 48).Trim('_') }
    if ([string]::IsNullOrWhiteSpace($slug)) { return 'item' }
    return $slug
}

function New-IdPart {
    param([string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    $part = ([string] $Value).ToLowerInvariant() -replace '[^a-z0-9]', ''
    return $part
}

function New-ConnectorId {
    param(
        [string] $Manufacturer,
        [string] $Model,
        [string] $FirmwareFamily,
        [object[]] $Candidates,
        [object[]] $FieldRecords
    )

    $manufacturerPart = New-IdPart -Value $Manufacturer
    $modelPart = New-IdPart -Value $Model
    $firmwarePart = New-IdPart -Value $FirmwareFamily

    # Permanent ids are based only on observed identity (never on IP, host,
    # serial, MAC or any other device-specific value).
    if ($manufacturerPart -and $modelPart) {
        $id = $manufacturerPart + '.' + $modelPart
        if ($firmwarePart) { $id += '.fw-' + $firmwarePart }
        return $id
    }

    # Unknown manufacturer/model gets an explicitly temporary observed.* id.
    # The hash is derived from non-sensitive identity evidence and field paths,
    # deliberately excluding the selected host and network information.
    $signature = @()
    foreach ($candidate in @($Candidates)) {
        $metric = [string] (Get-Prop -Object $candidate -Name 'metric')
        if ($metric -notin @('device.manufacturer', 'device.model', 'device.firmware', 'device.hardware', 'device.operator')) { continue }
        $value = Get-Prop -Object $candidate -Name 'value'
        if ($null -eq $value -or -not (Test-SafeIdentifierValue -Value $value)) { continue }
        $signature += ([string] $metric) + '|' + ([string] (Get-Prop -Object $candidate -Name 'path')) + '|' + ([string] (Get-Prop -Object $candidate -Name 'field')) + '|' + ([string] $value)
    }
    if ($signature.Count -eq 0) {
        foreach ($record in @($FieldRecords)) {
            if ([string] (Get-Prop -Object $record -Name 'kind') -ne 'scalar') { continue }
            $signature += ([string] (Get-Prop -Object $record -Name 'path')) + '|' + ([string] (Get-Prop -Object $record -Name 'name'))
        }
    }
    $digest = Get-StableHash -Text (($signature | Sort-Object) -join '|') -Length 10
    return 'observed.' + $(if ($modelPart) { $modelPart } else { 'capture' }) + '-' + $digest
}

function New-EndpointId {
    # Endpoint ids match the Python reference analyzer: <method>_<path-slug>,
    # with _02, _03, ... added by the caller for repeated shapes.
    param([string] $Method, [string] $Path)
    return ((New-Slug -Text $Method) + '_' + (New-Slug -Text $Path))
}

function Ensure-Directory {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Write-JsonFile {
    param([string] $Path, [object] $Data)
    $json = $Data | ConvertTo-Json -Depth 40
    if (-not (Test-OutputSafety -Text $json)) {
        throw "Refusing to write an output file because a sensitive-looking pattern remained; nothing was written for that file."
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, $encoding)
}

# ---------------------------------------------------------------------------
# Mandatory sanitization
# ---------------------------------------------------------------------------
function Get-NamePlaceholder {
    param([string] $Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
    $n = $Name.ToLowerInvariant()
    if ($n -match '(pass(word|wd)?|passwd|pwd|secret|loginpd|credential)') { return '<redacted-password>' }
    if ($n -match '(authorization|proxy-authorization|csrf|xsrf|token|api[-_]?key|apikey|bearer|authkey)') { return '<redacted-token>' }
    if ($n -match '(session|sessionid|cookies?|set-cookie|jsessionid|phpsessid|\bsid\b)') { return '<redacted-session>' }
    if ($n -match '(nonce|challenge|random|acsrandom|seed|salt)') { return '<redacted-nonce>' }
    if ($n -match '(serial|gponsn|^sn$|serialnumber)') { return '<redacted-serial>' }
    if ($n -match '(^mac$|macaddr|macaddress|hwaddr|brmac)') { return '<redacted-mac>' }
    if ($n -match '(phone|telephone|msisdn|mobile|directorynumber|^tel$|^uri[0-9]*$)') { return '<redacted-phone>' }
    if ($n -match '(hostname|host_name|devicename|device_name|computername|friendlyname)') { return '<redacted-hostname>' }
    if ($n -match '(username|user|account|pppoe|loginname|authusername|user_name)') { return '<redacted-credential>' }
    return $null
}

function Test-PrivateIPv4 {
    param([string] $Value)
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($Value, [ref] $ip)) { return $true }
    $bytes = $ip.GetAddressBytes()
    if ($bytes.Length -ne 4) { return $true }
    $a = [int] $bytes[0]
    $b = [int] $bytes[1]
    if ($a -eq 0 -or $a -eq 10 -or $a -eq 127) { return $true }
    if ($a -eq 169 -and $b -eq 254) { return $true }
    if ($a -eq 172 -and $b -ge 16 -and $b -le 31) { return $true }
    if ($a -eq 192 -and $b -eq 168) { return $true }
    if ($a -eq 100 -and $b -ge 64 -and $b -le 127) { return $true }
    if ($a -eq 192 -and $b -eq 0) { return $true }
    if ($a -eq 198 -and ($b -eq 18 -or $b -eq 19)) { return $true }
    if ($a -eq 198 -and $b -eq 51) { return $true }
    if ($a -ge 224) { return $true }
    return $false
}

function Invoke-RedactText {
    param([string] $Text, [string] $FieldName = '')
    if ($null -eq $Text) { return $Text }
    $s = [string] $Text
    if ([string]::IsNullOrEmpty($s)) { return $s }

    $namePlaceholder = Get-NamePlaceholder -Name $FieldName
    if ($null -ne $namePlaceholder) {
        Register-Redaction -Placeholder $namePlaceholder
        Register-SensitiveLiteral -Value $s
        return $namePlaceholder
    }

    $macPattern = '(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b'
    $macMatches = [regex]::Matches($s, $macPattern)
    if ($macMatches.Count -gt 0) {
        Register-Redaction -Placeholder '<redacted-mac>' -Count $macMatches.Count
        foreach ($match in $macMatches) { Register-SensitiveLiteral -Value $match.Value }
        $s = [regex]::Replace($s, $macPattern, '<redacted-mac>')
    }

    $ipPattern = '(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'
    foreach ($match in @([regex]::Matches($s, $ipPattern))) {
        $value = $match.Value
        $isPublic = $false
        try { $isPublic = -not (Test-PrivateIPv4 -Value $value) } catch { $isPublic = $false }
        $placeholder = if ($isPublic) { '<redacted-public-ip>' } else { '<redacted-ip>' }
        Register-Redaction -Placeholder $placeholder
        Register-SensitiveLiteral -Value $value
        $s = $s.Replace($value, $placeholder)
    }

    $phonePattern = '(?<![\w.])(?:\+\d[\d\-\s().]{7,17}\d|\d{2,4}[-.\s]\d{3,4}[-.\s]\d{4})(?![\w.])'
    $phoneMatches = [regex]::Matches($s, $phonePattern)
    if ($phoneMatches.Count -gt 0) {
        Register-Redaction -Placeholder '<redacted-phone>' -Count $phoneMatches.Count
        foreach ($match in $phoneMatches) { Register-SensitiveLiteral -Value $match.Value }
        $s = [regex]::Replace($s, $phonePattern, '<redacted-phone>')
    }

    $serialPattern = '(?<![A-Za-z0-9_])(?=[A-Za-z0-9]{12,24}\b)(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9]{12,24}(?![A-Za-z0-9_])'
    foreach ($match in @([regex]::Matches($s, $serialPattern))) {
        Register-Redaction -Placeholder '<redacted-serial>'
        Register-SensitiveLiteral -Value $match.Value
        $s = $s.Replace($match.Value, '<redacted-serial>')
    }

    $tokenPattern = '(?<![A-Za-z0-9_/+=_-])(?=[A-Za-z0-9+/=_-]{20,})(?=[A-Za-z0-9+/=_-]*[A-Z])(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]+(?![A-Za-z0-9_/+=_-])'
    foreach ($match in @([regex]::Matches($s, $tokenPattern))) {
        Register-Redaction -Placeholder '<redacted-session>'
        Register-SensitiveLiteral -Value $match.Value
        $s = $s.Replace($match.Value, '<redacted-session>')
    }

    return $s
}

function Redact-Scalar {
    param($Value, [string] $FieldName = '')
    if ($null -eq $Value) { return $null }
    if ($Value -is [ValueType]) {
        $namePlaceholder = Get-NamePlaceholder -Name $FieldName
        if ($null -ne $namePlaceholder) {
            Register-Redaction -Placeholder $namePlaceholder
            return $namePlaceholder
        }
        return $Value
    }
    return Invoke-RedactText -Text ([string] $Value) -FieldName $FieldName
}

function Test-OutputSafety {
    param([string] $Text)
    if ([string]::IsNullOrEmpty($Text)) { return $true }
    foreach ($literal in $script:SensitiveLiterals) {
        if (-not [string]::IsNullOrWhiteSpace($literal) -and $Text.Contains($literal)) {
            return $false
        }
    }
    $patterns = @(
        '(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b',
        '(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',
        '(?<![A-Za-z0-9_/+=_-])(?=[A-Za-z0-9+/=_-]{20,})(?=[A-Za-z0-9+/=_-]*[A-Z])(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]+(?![A-Za-z0-9_/+=_-])'
    )
    foreach ($pattern in $patterns) {
        if ([regex]::IsMatch($Text, $pattern)) { return $false }
    }
    return $true
}

function Test-SafeIdentifierValue {
    param($Value)
    if ($null -eq $Value) { return $false }
    $s = [string] $Value
    if ([string]::IsNullOrWhiteSpace($s) -or $s.Length -gt 48) { return $false }
    if ($s -match '\d{6,}') { return $false }
    $redacted = Invoke-RedactText -Text $s
    if ($redacted -ne $s) { return $false }
    return $true
}

function Build-Structure {
    param($Value, [string] $FieldName = '', [int] $Depth = 0)
    if ($Depth -gt 12) { return '<max-depth>' }
    if ($null -eq $Value) { return '<null>' }
    if ($Value -is [string] -or $Value -is [ValueType]) {
        return Redact-Scalar -Value $Value -FieldName $FieldName
    }

    $isDictionary = $Value -is [System.Collections.IDictionary]
    $isEnumerable = ($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])
    if ($isEnumerable -and -not $isDictionary) {
        $items = @(To-Array $Value)
        $samples = @()
        if ($items.Count -gt 0) {
            $limit = [Math]::Min($items.Count, 3) - 1
            for ($i = 0; $i -le $limit; $i++) {
                $samples += ,(Build-Structure -Value $items[$i] -FieldName $FieldName -Depth ($Depth + 1))
            }
        }
        if ($items.Count -gt 3) {
            return [ordered]@{
                '__count'  = $items.Count
                '__sample' = @($samples)
            }
        }
        return @($samples)
    }

    $names = @(Get-PropNames -Object $Value)
    $output = [ordered]@{}
    if ($names.Count -eq 0) { return $output }
    $limit = [Math]::Min($names.Count, 80) - 1
    for ($i = 0; $i -le $limit; $i++) {
        $name = [string] $names[$i]
        $safeName = Invoke-RedactText -Text $name
        $output[$safeName] = Build-Structure -Value (Get-Prop -Object $Value -Name $name) -FieldName $name -Depth ($Depth + 1)
    }
    if ($names.Count -gt 80) {
        $output['__note'] = '<additional fields omitted>'
    }
    return $output
}

# ---------------------------------------------------------------------------
# Response/request inspection (raw values stay in memory only)
# ---------------------------------------------------------------------------
function Collect-FieldPaths {
    param(
        $Value,
        [string] $Prefix = '',
        [string] $FieldName = '',
        [int] $Depth = 0,
        [System.Collections.Generic.List[object]] $Results
    )
    if ($Depth -gt 10 -or $null -eq $Value) { return }
    if ($Value -is [string] -or $Value -is [ValueType]) {
        if (-not [string]::IsNullOrWhiteSpace($Prefix)) {
            $Results.Add([ordered]@{
                path  = Invoke-RedactText -Text $Prefix
                name  = Invoke-RedactText -Text $FieldName
                value = $Value
                kind  = 'scalar'
            })
        }
        return
    }

    $isDictionary = $Value -is [System.Collections.IDictionary]
    $isEnumerable = ($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])
    if ($isEnumerable -and -not $isDictionary) {
        $items = @(To-Array $Value)
        if ($items.Count -gt 0) {
            $sample = $items[0]
            $samplePath = if ([string]::IsNullOrEmpty($Prefix)) { '$[0]' } else { $Prefix + '[0]' }
            if (($sample -is [System.Collections.IDictionary]) -or ($sample -is [PSCustomObject])) {
                $Results.Add([ordered]@{
                    path        = Invoke-RedactText -Text $Prefix
                    name        = Invoke-RedactText -Text $FieldName
                    value       = $null
                    kind        = 'array'
                    item_fields = @(Get-PropNames -Object $sample | ForEach-Object { Invoke-RedactText -Text ([string] $_) })
                })
            }
            Collect-FieldPaths -Value $sample -Prefix $samplePath -FieldName $FieldName -Depth ($Depth + 1) -Results $Results
        }
        return
    }

    foreach ($name in @(Get-PropNames -Object $Value)) {
        $nextPrefix = if ([string]::IsNullOrEmpty($Prefix)) { '$.' + $name } else { $Prefix + '.' + $name }
        $child = Get-Prop -Object $Value -Name $name
        if ($null -eq $child) { continue }
        # Get-Prop preserves arrays with a leading comma, so the array shape is
        # visible here without further wrapping.
        Collect-FieldPaths -Value $child -Prefix $nextPrefix -FieldName ([string] $name) -Depth ($Depth + 1) -Results $Results
    }
}

function Collect-RawValues {
    param(
        $Value,
        [int] $Depth = 0,
        [System.Collections.Generic.HashSet[string]] $Values,
        [System.Collections.Generic.HashSet[string]] $Fields
    )
    if ($Depth -gt 10 -or $null -eq $Value) { return }
    if ($Value -is [string]) {
        $s = [string] $Value
        if ($s.Length -ge 6) { $Values.Add($s) | Out-Null }
        return
    }
    if ($Value -is [ValueType]) { return }

    $isDictionary = $Value -is [System.Collections.IDictionary]
    $isEnumerable = ($Value -is [System.Collections.IEnumerable]) -and -not ($Value -is [string])
    if ($isEnumerable -and -not $isDictionary) {
        $items = @(To-Array $Value)
        $limit = [Math]::Min($items.Count, 20) - 1
        for ($i = 0; $i -le $limit; $i++) {
            Collect-RawValues -Value $items[$i] -Depth ($Depth + 1) -Values $Values -Fields $Fields
        }
        return
    }
    foreach ($name in @(Get-PropNames -Object $Value)) {
        $Fields.Add([string] $name) | Out-Null
        Collect-RawValues -Value (Get-Prop -Object $Value -Name $name) -Depth ($Depth + 1) -Values $Values -Fields $Fields
    }
}

function Get-SanitizedUrlInfo {
    param([Uri] $Uri)
    $query = [ordered]@{}
    $names = @()
    foreach ($part in @($Uri.Query.TrimStart('?') -split '&')) {
        if ([string]::IsNullOrWhiteSpace($part)) { continue }
        $pair = $part -split '=', 2
        $rawName = [Uri]::UnescapeDataString($pair[0])
        if ([string]::IsNullOrWhiteSpace($rawName)) { continue }
        $rawValue = if ($pair.Count -gt 1) { [Uri]::UnescapeDataString($pair[1]) } else { '' }
        $safeName = Invoke-RedactText -Text $rawName
        $names += $safeName
        $query[$safeName] = Invoke-RedactText -Text $rawValue -FieldName $rawName
    }
    $path = Invoke-RedactText -Text $Uri.AbsolutePath
    if ([string]::IsNullOrWhiteSpace($path)) { $path = '/' }
    $scheme = if ($Uri.Scheme -eq 'https') { 'https' } else { 'http' }
    return [ordered]@{
        scheme       = $scheme
        path         = $path
        query        = $query
        query_names  = @($names | Select-Object -Unique)
        sanitized    = $scheme + '://<selected-host>' + $path
    }
}

function Get-RawQueryMap {
    param([Uri] $Uri, $Request)
    $map = [ordered]@{}
    foreach ($part in @($Uri.Query.TrimStart('?') -split '&')) {
        if ([string]::IsNullOrWhiteSpace($part)) { continue }
        $pair = $part -split '=', 2
        $name = [Uri]::UnescapeDataString($pair[0])
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $map[$name] = if ($pair.Count -gt 1) { [Uri]::UnescapeDataString($pair[1]) } else { '' }
    }
    foreach ($item in @(To-Array (Get-Prop -Object $Request -Name 'queryString'))) {
        $name = [string] (Get-Prop -Object $item -Name 'name')
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $map[$name] = [string] (Get-Prop -Object $item -Name 'value')
        }
    }
    return $map
}

function Get-RequestPostData {
    param($Request)
    $post = Get-Prop -Object $Request -Name 'postData'
    $result = [ordered]@{ kind = 'none'; object = $null; mime = '' }
    if ($null -eq $post) { return $result }
    $mime = [string] (Get-Prop -Object $post -Name 'mimeType')
    $result['mime'] = $mime
    $text = [string] (Get-Prop -Object $post -Name 'text')
    if ($mime -match 'json' -and -not [string]::IsNullOrWhiteSpace($text)) {
        try {
            $result['object'] = $text | ConvertFrom-Json
            $result['kind'] = 'json'
            return $result
        } catch {
            # Fall back to a structural view of the text below.
        }
    }
    $params = @(To-Array (Get-Prop -Object $post -Name 'params'))
    if ($params.Count -gt 0) {
        $obj = [ordered]@{}
        foreach ($param in $params) {
            $name = [string] (Get-Prop -Object $param -Name 'name')
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $obj[$name] = [string] (Get-Prop -Object $param -Name 'value')
            }
        }
        $result['object'] = $obj
        $result['kind'] = 'form'
        return $result
    }
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        $result['object'] = [ordered]@{ '<raw-body-structure-omitted>' = '<redacted>' }
        $result['kind'] = if ($mime -match 'json') { 'json' } else { 'form' }
    }
    return $result
}

function Get-ResponseFormat {
    param([string] $Mime, $Parsed)
    if ($Mime -match 'json') { return 'json' }
    if ($Mime -match 'html') { return 'html' }
    if ($Mime -match 'xml') { return 'xml' }
    if ($null -ne $Parsed) { return 'json' }
    return 'text'
}

function Get-ResponseText {
    param($Response)
    $content = Get-Prop -Object $Response -Name 'content'
    $text = [string] (Get-Prop -Object $content -Name 'text')
    $encoding = [string] (Get-Prop -Object $content -Name 'encoding')
    if ($encoding -eq 'base64' -and -not [string]::IsNullOrEmpty($text)) {
        try {
            $bytes = [Convert]::FromBase64String($text)
            $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        } catch {
            $text = ''
        }
    }
    return $text
}

function Split-HostAndPort {
    param([string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return [ordered]@{ host = $null; port = $null } }
    $v = $Value.Trim()
    if ($v -match '^[A-Za-z][A-Za-z0-9+.-]*://') {
        $uri = $null
        if ([Uri]::TryCreate($v, [UriKind]::Absolute, [ref] $uri)) {
            $port = $null
            if (-not $uri.IsDefaultPort) { $port = $uri.Port }
            return [ordered]@{ host = $uri.Host; port = $port }
        }
    }
    if ($v -match '^\[([^\]]+)\](?::(\d+))?$') {
        $port = $null
        if ($Matches[2]) { $port = [int] $Matches[2] }
        return [ordered]@{ host = $Matches[1]; port = $port }
    }
    if ($v -match '^([^:]+)(?::(\d+))?$') {
        $port = $null
        if ($Matches[2]) { $port = [int] $Matches[2] }
        return [ordered]@{ host = $Matches[1]; port = $port }
    }
    return [ordered]@{ host = $v; port = $null }
}

function Get-NetworkInfo {
    $info = [ordered]@{
        interface  = '<not detected>'
        windows_ip = '<not detected>'
        prefix     = '<not detected>'
        gateway    = $null
    }
    try {
        $routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object RouteMetric, InterfaceMetric)
    } catch {
        $routes = @()
    }
    if ($routes.Count -eq 0) { return $info }

    $route = $routes[0]
    $index = [int] $route.InterfaceIndex
    $info['gateway'] = [string] $route.NextHop
    try {
        $config = Get-NetIPConfiguration -InterfaceIndex $index -ErrorAction Stop
        if ($null -ne $config) {
            $alias = [string] $config.InterfaceAlias
            if ([string]::IsNullOrWhiteSpace($alias)) { $alias = [string] $config.InterfaceDescription }
            if (-not [string]::IsNullOrWhiteSpace($alias)) { $info['interface'] = $alias }
            $addresses = @($config.IPv4Address | Where-Object { $null -ne $_.IPAddress })
            if ($addresses.Count -gt 0) {
                $info['windows_ip'] = [string] $addresses[0].IPAddress
                $info['prefix'] = [string] $addresses[0].PrefixLength
            }
        }
    } catch {
        # Fall back to Get-NetIPAddress below.
    }
    if ($info['windows_ip'] -eq '<not detected>') {
        try {
            $address = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.IPAddress -ne '127.0.0.1' } | Select-Object -First 1)
            if ($address.Count -gt 0) {
                $info['windows_ip'] = [string] $address[0].IPAddress
                $info['prefix'] = [string] $address[0].PrefixLength
            }
        } catch {
            # Kept as not detected.
        }
    }
    return $info
}

function Test-TargetMatch {
    param([Uri] $Uri, [string] $TargetHost, $TargetPort)
    $entryHost = $Uri.Host.ToLowerInvariant().TrimEnd('.')
    if ($entryHost -ne $TargetHost) { return $false }
    if ($null -ne $TargetPort -and [int] $Uri.Port -ne [int] $TargetPort) { return $false }
    return $true
}

# ---------------------------------------------------------------------------
# Auth and request-shape analysis
# ---------------------------------------------------------------------------
function Get-AuthSignalsForEntry {
    param($Wrapper)
    $signals = [System.Collections.Generic.List[string]]::new()
    foreach ($header in @($Wrapper.headers)) {
        $name = ([string] (Get-Prop -Object $header -Name 'name')).ToLowerInvariant()
        $value = [string] (Get-Prop -Object $header -Name 'value')
        if ($name -eq 'authorization') {
            if ($value -match '^Basic\s+') { $signals.Add('http_basic') }
            elseif ($value -match '^Digest\s+') { $signals.Add('http_digest') }
            elseif ($value -match '^Bearer\s+') { $signals.Add('token') }
            else { $signals.Add('token') }
        } elseif ($name -in @('cookie', 'set-cookie')) {
            $signals.Add('session')
        } elseif ($name -match '(csrf|xsrf|nonce|api[-_]?key|token)') {
            $signals.Add('token')
        }
    }
    foreach ($name in @($Wrapper.query_raw.Keys)) {
        $lower = ([string] $name).ToLowerInvariant()
        if ($lower -match '(session|csrf|xsrf|token|nonce|challenge|random|seed)') { $signals.Add('dynamic_value') }
    }
    foreach ($name in @(Get-PropNames -Object $Wrapper.body_obj)) {
        $lower = ([string] $name).ToLowerInvariant()
        if ($lower -match '(password|loginpd|secret|credential)') { $signals.Add('form_credentials') }
        if ($lower -match '(session|csrf|xsrf|token|nonce|challenge|random|seed|loginpd|encrypted|cipher)') { $signals.Add('dynamic_value') }
    }
    $path = ([string] $Wrapper.url_info.path).ToLowerInvariant()
    if ($path -match '(login|signin|sign-in|auth|session|challenge|random|seed|acs)') {
        if ([string] $Wrapper.method -eq 'POST') { $signals.Add('login_post') } else { $signals.Add('preauth_helper') }
    }
    return @($signals | Select-Object -Unique)
}

function Get-Purpose {
    param($Wrapper)
    $p = ([string] $Wrapper.url_info.path).ToLowerInvariant()
    if ($p -match '(login|signin|sign-in|auth)') { return 'authentication' }
    if ($p -match '(seed|random|challenge|acs)') { return 'challenge-or-seed observation' }
    if ($p -match '(wan|internet|ip)') { return 'WAN status observation' }
    if ($p -match '(optical|pon|gpon|fiber)') { return 'optical status observation' }
    if ($p -match '(voice|voip|sip|tel)') { return 'VoIP status observation' }
    if ($p -match '(device|base|system|info|version|status)') { return 'device or system observation' }
    if ($p -match '(lan|client|device|host)') { return 'connected-device observation' }
    return 'other observed request'
}

function Get-DependencyEndpointIds {
    param([string] $FieldName, [string] $RawValue)
    $ids = @()
    foreach ($group in @($script:EarlierGroups)) {
        if (-not [string]::IsNullOrWhiteSpace($FieldName) -and $group.fields.Contains($FieldName)) {
            $ids += [string] $group.id
        } elseif (-not [string]::IsNullOrWhiteSpace($RawValue) -and $RawValue.Length -ge 6 -and $group.values.Contains($RawValue)) {
            $ids += [string] $group.id
        }
    }
    return @($ids | Select-Object -Unique)
}

function Add-DependencyRecord {
    param(
        [System.Collections.Generic.List[object]] $Records,
        [string] $EndpointId,
        [string] $FieldName,
        [string[]] $DependsOn = @()
    )
    foreach ($dependency in @($DependsOn)) {
        if ([string]::IsNullOrWhiteSpace($dependency) -or $dependency -eq $EndpointId) { continue }
        $found = $false
        foreach ($record in $Records) {
            if ($record.endpoint -eq $EndpointId -and $record.dependency -eq $dependency) { $found = $true; break }
        }
        if (-not $found) {
            $Records.Add([ordered]@{ endpoint = $EndpointId; dependency = $dependency; field = $FieldName })
        }
    }
}

function Build-RequestValue {
    param(
        $Value,
        [string] $FieldName,
        [System.Collections.Generic.HashSet[string]] $EarlierValues,
        [System.Collections.Generic.HashSet[string]] $EarlierFieldNames,
        [string] $EndpointId,
        [System.Collections.Generic.List[object]] $Dependencies
    )
    if ($Value -is [System.Collections.IDictionary] -or $Value -is [PSCustomObject]) {
        $out = [ordered]@{}
        foreach ($name in @(Get-PropNames -Object $Value)) {
            $out[(Invoke-RedactText -Text ([string] $name))] = Build-RequestValue -Value (Get-Prop -Object $Value -Name $name) -FieldName ([string] $name) -EarlierValues $EarlierValues -EarlierFieldNames $EarlierFieldNames -EndpointId $EndpointId -Dependencies $Dependencies
        }
        return $out
    }
    if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
        $list = @()
        foreach ($item in @(To-Array $Value)) {
            $list += ,(Build-RequestValue -Value $item -FieldName $FieldName -EarlierValues $EarlierValues -EarlierFieldNames $EarlierFieldNames -EndpointId $EndpointId -Dependencies $Dependencies)
        }
        return @($list)
    }

    $namePlaceholder = Get-NamePlaceholder -Name $FieldName
    if ($null -ne $namePlaceholder) {
        Register-Redaction -Placeholder $namePlaceholder
        $target = if ($namePlaceholder -eq '<redacted-password>') { 'password' } elseif ($namePlaceholder -eq '<redacted-credential>') { 'username' } else { (New-Slug $FieldName) }
        return [ordered]@{ source = 'user_input'; field = $target }
    }

    $raw = if ($null -eq $Value) { '' } else { [string] $Value }
    $dependencyIds = @(Get-DependencyEndpointIds -FieldName $FieldName -RawValue '')
    if ($dependencyIds.Count -gt 0 -and $FieldName -match '(session|token|csrf|nonce|challenge|random|seed|id$|sid$)') {
        Add-DependencyRecord -Records $Dependencies -EndpointId $EndpointId -FieldName $FieldName -DependsOn $dependencyIds
        return [ordered]@{ source = 'state'; field = (Invoke-RedactText -Text $FieldName) }
    }
    $valueDependencyIds = @(Get-DependencyEndpointIds -FieldName '' -RawValue $raw)
    if ($valueDependencyIds.Count -gt 0 -and $raw.Length -ge 6) {
        Add-DependencyRecord -Records $Dependencies -EndpointId $EndpointId -FieldName $FieldName -DependsOn $valueDependencyIds
        return [ordered]@{ source = 'state'; field = (Invoke-RedactText -Text $FieldName) }
    }
    $redacted = Invoke-RedactText -Text $raw -FieldName $FieldName
    if ($redacted -ne $raw) {
        return [ordered]@{ source = 'state'; field = (Invoke-RedactText -Text $FieldName) }
    }
    return $raw
}

function Build-SanitizedHeaders {
    param($Headers)
    $out = [ordered]@{}
    foreach ($header in @($Headers)) {
        $name = Invoke-RedactText -Text ([string] (Get-Prop -Object $header -Name 'name'))
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $lower = $name.ToLowerInvariant()
        if ($lower -in @('content-type', 'accept', 'x-requested-with', 'accept-language')) {
            $out[$name] = Invoke-RedactText -Text ([string] (Get-Prop -Object $header -Name 'value')) -FieldName $name
        } else {
            $placeholder = Get-NamePlaceholder -Name $lower
            if ($null -ne $placeholder) {
                Register-Redaction -Placeholder $placeholder
                $out[$name] = $placeholder
            } else {
                $out[$name] = '<redacted-value>'
            }
        }
    }
    return $out
}

function Build-QueryDefinition {
    param(
        $Query,
        [System.Collections.Generic.HashSet[string]] $EarlierValues,
        [System.Collections.Generic.HashSet[string]] $EarlierFieldNames,
        [string] $EndpointId,
        [System.Collections.Generic.List[object]] $Dependencies
    )
    $out = [ordered]@{}
    foreach ($name in @($Query.Keys)) {
        $raw = [string] $Query[$name]
        $value = Build-RequestValue -Value $raw -FieldName ([string] $name) -EarlierValues $EarlierValues -EarlierFieldNames $EarlierFieldNames -EndpointId $EndpointId -Dependencies $Dependencies
        $out[(Invoke-RedactText -Text ([string] $name))] = $value
    }
    return $out
}

function Get-RepresentativeEntry {
    param($Group)
    $candidates = @($Group.entries | Sort-Object -Property @(
        @{ Expression = { if ($null -ne $_.parsed) { 1 } else { 0 } } },
        @{ Expression = { [int] $_.text_length } }
    ) -Descending)
    if ($candidates.Count -eq 0) { return $Group.entries[0] }
    return $candidates[0]
}

function New-EndpointDefinition {
    param(
        $Group,
        $EarlierValues,
        $EarlierFieldNames,
        [System.Collections.Generic.List[object]] $Dependencies
    )
    $rep = Get-RepresentativeEntry -Group $Group
    $method = [string] $rep.method
    $definition = [ordered]@{
        id              = [string] $Group.id
        method          = $method
        path            = [string] $rep.url_info.path
        format          = [string] $rep.format
        timeout_seconds = 6
        scheme          = [string] $rep.url_info.scheme
        auth            = [bool] $rep.endpoint_auth
        description     = 'Observed in HAR and sanitized; verify path, parameters and auth before enabling.'
    }
    $query = Build-QueryDefinition -Query $rep.query_raw -EarlierValues $EarlierValues -EarlierFieldNames $EarlierFieldNames -EndpointId $Group.id -Dependencies $Dependencies
    if ($query.Count -gt 0) { $definition['query'] = $query }

    if ($method -eq 'POST') {
        $body = if ($null -eq $rep.body_obj) { [ordered]@{} } else { Build-RequestValue -Value $rep.body_obj -FieldName '' -EarlierValues $EarlierValues -EarlierFieldNames $EarlierFieldNames -EndpointId $Group.id -Dependencies $Dependencies }
        $definition['body'] = $body
        $definition['body_format'] = if ([string] $rep.body_kind -eq 'json') { 'json' } else { 'form' }
    }
    $headers = Build-SanitizedHeaders -Headers $rep.headers
    if ($headers.Count -gt 0) { $definition['headers'] = $headers }
    return $definition
}

function Get-XmlFieldNames {
    param([string] $Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return @() }
    $names = [System.Collections.Generic.HashSet[string]]::new()
    $reader = $null
    try {
        $settings = [System.Xml.XmlReaderSettings]::new()
        $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $reader = [System.Xml.XmlReader]::Create([System.IO.StringReader]::new($Text), $settings)
        $document = [System.Xml.XmlDocument]::new()
        $document.XmlResolver = $null
        $document.Load($reader)
        foreach ($node in @($document.SelectNodes('//*'))) {
            if ($null -ne $node.LocalName) { [void] $names.Add([string] $node.LocalName) }
        }
    } catch {
        return @()
    } finally {
        if ($null -ne $reader) { $reader.Dispose() }
    }
    return @($names | Sort-Object)
}

function Get-UnsupportedContentNote {
    param($Wrapper)
    $format = [string] $Wrapper.format
    if ($format -eq 'json') { return $null }
    if ($format -eq 'xml') {
        return [ordered]@{
            format                = 'xml'
            fields_not_analyzed   = @(Get-XmlFieldNames -Text ([string] $Wrapper.response_text))
            reason                = 'XML element names were observed for evidence, but the connector runtime has no XML extraction method. No extraction candidate was generated; manual extraction work is required.'
        }
    }
    return [ordered]@{
        format              = $format
        fields_not_analyzed = @('<body>')
        reason              = 'The runtime has no safe HTML extraction method. The response was not parsed into field paths and no extraction candidate was generated; manual extraction work is required.'
    }
}

function New-EndpointEvidence {
    param($Group)
    $rep = Get-RepresentativeEntry -Group $Group
    $evidence = [ordered]@{
        endpoint_id              = [string] $Group.id
        method                   = [string] $rep.method
        path                     = [string] $rep.url_info.path
        scheme                   = [string] $rep.url_info.scheme
        format                   = [string] $rep.format
        representative_status    = [int] (Get-Prop -Object $rep.response -Name 'status')
        unsupported_content      = Get-UnsupportedContentNote -Wrapper $rep
        response_structure       = $null
        request_structure        = [ordered]@{
            query   = Build-SanitizedHeaders -Headers @()
            headers = Build-SanitizedHeaders -Headers $rep.headers
        }
        notes                    = @()
    }
    if ($null -ne $rep.parsed) {
        $evidence['response_structure'] = Build-Structure -Value $rep.parsed
    } else {
        $evidence['notes'] += 'No parseable JSON response content was present in the HAR for this endpoint.'
        if ($null -ne $evidence['unsupported_content']) {
            $evidence['notes'] += ([string] $evidence['unsupported_content'].reason)
        }
    }
    $queryStructure = [ordered]@{}
    foreach ($name in @($rep.url_info.query.Keys)) {
        $queryName = Invoke-RedactText -Text ([string] $name)
        $queryStructure[$queryName] = Invoke-RedactText -Text ([string] $rep.url_info.query[$name]) -FieldName ([string] $name)
    }
    $evidence['request_structure']['query'] = $queryStructure
    if ([string] $rep.method -eq 'POST') {
        $evidence['request_structure']['body'] = Build-Structure -Value $rep.body_obj
        $evidence['request_structure']['body_format'] = [string] $rep.body_kind
    }
    $evidence['notes'] += 'All values are sanitized; the raw HAR was never copied.'
    return $evidence
}

# ---------------------------------------------------------------------------
# Metric candidate mapping (existing universal metric IDs only)
# ---------------------------------------------------------------------------
function Get-MappingTable {
    # This is the same conservative mapping as tools/har_analyzer.py. Field
    # names alone never establish units, transforms or a semantic equivalence.
    return @(
        @{ name = '(manufacturer|vendor|make)'; path = $null; metric = 'device.manufacturer'; confidence = 'partial' }
        @{ name = '(model|productname|devicemodel)'; path = $null; metric = 'device.model'; confidence = 'partial' }
        @{ name = '(firmware|softwareversion|version)'; path = $null; metric = 'device.firmware'; confidence = 'partial' }
        @{ name = '(hardware|hwversion|hwver)'; path = $null; metric = 'device.hardware'; confidence = 'partial' }
        @{ name = '(wanstatus|linkstatus|connectionstatus|wanstate)'; path = $null; metric = 'wan.status'; confidence = 'partial' }
        @{ name = '(wanip|publicip|internetip|wanaddr)'; path = $null; metric = 'wan.public_ip'; confidence = 'partial' }
        @{ name = '(wanuptime|connectionuptime|uptime)'; path = $null; metric = 'wan.uptime_s'; confidence = 'partial' }
        @{ name = '(rxpower|rxpowerdbm|receivepower)'; path = $null; metric = 'optical.rx_power_dbm'; confidence = 'partial' }
        @{ name = '(txpower|txpowerdbm|transmitpower)'; path = $null; metric = 'optical.tx_power_dbm'; confidence = 'partial' }
        @{ name = '(temperature|opticaltemp|tempc)'; path = $null; metric = 'optical.temperature_c'; confidence = 'partial' }
        @{ name = '(phonenumber|telnumber|sipuser|voipnumber)'; path = $null; metric = 'voip.phone_number'; confidence = 'partial' }
        @{ name = '(registration|regstate|voipstatus)'; path = $null; metric = 'voip.registration_state'; confidence = 'partial' }
        @{ name = '(uptime)'; path = $null; metric = 'system.uptime_s'; confidence = 'partial' }
        @{ name = '(cpuusage|cpupercent|cpu)'; path = $null; metric = 'system.cpu_percent'; confidence = 'partial' }
        @{ name = '(memoryused|memorypercent|memory)'; path = $null; metric = 'system.memory_percent'; confidence = 'partial' }
        @{ name = '(clients|devices|stations|lanhosts|clientlist)'; path = $null; metric = 'devices.count'; confidence = 'partial' }
    )
}

function New-ExtractionArtifacts {
    param([object[]] $FieldPathRecords)
    $rules = [System.Collections.Generic.List[object]]::new()
    $candidates = [System.Collections.Generic.List[object]]::new()
    $mapping = @(Get-MappingTable)
    $seen = @{}
    $usedRuleIds = @{}

    foreach ($record in $FieldPathRecords) {
        if ($record.kind -notin @('scalar', 'array')) { continue }
        $name = [string] $record.name
        $path = [string] $record.path
        $endpoint = [string] $record.endpoint
        if ([string]::IsNullOrWhiteSpace($name) -or [string]::IsNullOrWhiteSpace($path)) { continue }
        # A redacted value must never become a metric mapping candidate.
        if ($record.kind -eq 'scalar') {
            $safeValue = Invoke-RedactText -Text ([string] $record.value) -FieldName $name
            if ($safeValue -like '<redacted-*' -or $safeValue -ne [string] $record.value) { continue }
        }
        foreach ($entry in $mapping) {
            if ($name -notmatch $entry.name) { continue }
            if ($null -ne $entry.path -and $path -notmatch $entry.path) { continue }
            $metric = [string] $entry.metric
            $key = $endpoint + '|' + $metric + '|' + $path
            if ($seen.ContainsKey($key)) { break }
            $seen[$key] = $true
            $signature = $metric + '|' + $endpoint + '|' + $path
            $ruleId = 'extract-' + (New-Slug $metric) + '-' + (New-Slug $endpoint) + '-' + (Get-StableHash -Text $signature -Length 6)
            if ($usedRuleIds.ContainsKey($ruleId)) { continue }
            $usedRuleIds[$ruleId] = $true
            # Deliberately no transforms or value_type: a field name alone does
            # not prove units or encoding. A reviewer must confirm those.
            $rule = [ordered]@{
                id                      = $ruleId
                metric                  = $metric
                endpoint                = $endpoint
                method                  = 'json_path'
                expression              = $path
                availability_on_missing = 'unavailable'
            }
            $rules.Add($rule)
            $candidates.Add([ordered]@{
                metric         = $metric
                endpoint       = $endpoint
                field          = $name
                path           = $path
                method         = 'json_path'
                expression     = $path
                confidence     = [string] $entry.confidence
                manual_review  = $true
                evidence       = 'Field name and nesting matched a known metric name; meaning must be confirmed on the device.'
            })
            break
        }
    }

    # Memory usage is commonly split across total/free fields.
    $byEndpoint = @{}
    foreach ($record in $FieldPathRecords) {
        if ($record.kind -ne 'scalar') { continue }
        $key = [string] $record.endpoint
        if (-not $byEndpoint.ContainsKey($key)) { $byEndpoint[$key] = @() }
        $byEndpoint[$key] += $record
    }
    foreach ($endpoint in @($byEndpoint.Keys)) {
        $records = @($byEndpoint[$endpoint])
        $total = @($records | Where-Object { ([string] $_.name) -match '^mem_total$' } | Select-Object -First 1)
        $free = @($records | Where-Object { ([string] $_.name) -match '^mem_free$' } | Select-Object -First 1)
        if ($total.Count -eq 1 -and $free.Count -eq 1) {
            $expression = ([string] $total[0].path) + ',' + ([string] $free[0].path)
            $ruleId = 'extract-system-memory-' + (New-Slug $endpoint) + '-' + (Get-StableHash $expression 5)
            if (-not $usedRuleIds.ContainsKey($ruleId)) {
                # No extraction rule: combining totals into a percentage is a
                # transform that must be confirmed by a reviewer.
                $usedRuleIds[$ruleId] = $true
                $candidates.Add([ordered]@{
                    metric        = 'system.memory_percent'
                    endpoint      = $endpoint
                    field         = 'mem_total,mem_free'
                    path          = $expression
                    method        = 'json_path_multi'
                    expression    = $expression
                    confidence    = 'partial'
                    manual_review = $true
                    evidence      = 'Both total and free memory fields were observed; transform must be confirmed.'
                })
            }
        }
    }

    return [ordered]@{ rules = @($rules); candidates = @($candidates) }
}

function New-ValidationRules {
    param([object[]] $ExtractionRules)
    $validation = @()
    foreach ($rule in $ExtractionRules) {
        if (-not $rule.Contains('metric')) { continue }
        $metric = [string] $rule.metric
        $spec = $null
        if ($metric -match '^wan\.(public_ip|gateway_ip)$') { $spec = @{ rule = 'ipv4'; on_failure = 'invalid' } }
        elseif ($metric -match '^(device\.(mac_base|mac_wan)|wan\.mac)$') { $spec = @{ rule = 'mac'; on_failure = 'invalid' } }
        elseif ($metric -match '^optical\.(tx|rx)_power_dbm$') { $spec = @{ rule = 'dbm_range'; on_failure = 'invalid' } }
        elseif ($metric -eq 'voip.phone_number') { $spec = @{ rule = 'phone_e164'; on_failure = 'invalid' } }
        elseif ($metric -match '(uptime_s$)') { $spec = @{ rule = 'duration_seconds'; on_failure = 'invalid' } }
        elseif ($metric -match '^(device\.manufacturer|device\.model|device\.firmware|device\.hardware|wan\.status|optical\.registration_state|voip\.registration_state)$') { $spec = @{ rule = 'non_empty'; on_failure = 'unavailable' } }
        if ($null -eq $spec) { continue }
        $validation += [ordered]@{
            metric     = $metric
            rule       = $spec.rule
            on_failure = $spec.on_failure
        }
    }
    $unique = @()
    $seen = @{}
    foreach ($item in $validation) {
        $key = $item.metric + '|' + $item.rule
        if (-not $seen.ContainsKey($key)) { $seen[$key] = $true; $unique += $item }
    }
    return @($unique)
}

function Get-Capabilities {
    param([object[]] $ExtractionRules, [object[]] $FieldPathRecords)
    $metrics = @($ExtractionRules | Where-Object { $_.Contains('metric') } | ForEach-Object { [string] $_.metric })
    $caps = @()
    $identity = @('device.manufacturer', 'device.model', 'device.firmware', 'device.hardware', 'device.operator', 'device.mac_base', 'device.mac_wan', 'device.serial', 'device.gpon_sn')
    if (@($metrics | Where-Object { $identity -contains $_ }).Count -gt 0) { $caps += 'device_identity' }
    if (@($metrics | Where-Object { $_ -match '^wan\.' }).Count -gt 0) { $caps += 'wan' }
    if (@($metrics | Where-Object { $_ -match '^optical\.' }).Count -gt 0) { $caps += 'optical' }
    if (@($metrics | Where-Object { $_ -match '^voip\.' }).Count -gt 0) { $caps += 'voip' }
    if (@($metrics | Where-Object { $_ -match '^system\.' }).Count -gt 0) { $caps += 'system' }
    if (@($metrics | Where-Object { $_ -match '^devices\.|^speed_test\.' }).Count -gt 0) {
        if (@($metrics | Where-Object { $_ -match '^devices\.' }).Count -gt 0) { $caps += 'connected_devices' }
        if (@($metrics | Where-Object { $_ -match '^speed_test\.' }).Count -gt 0) { $caps += 'speed_test' }
    }
    if ($caps.Count -eq 0) { $caps += 'unclassified' }
    return @($caps | Select-Object -Unique)
}

# ---------------------------------------------------------------------------
# Identification and authentication classification
# ---------------------------------------------------------------------------
function New-IdentificationArtifacts {
    param([object[]] $FieldPathRecords, [hashtable] $EndpointAuth)
    $rules = [System.Collections.Generic.List[object]]::new()
    $candidates = [System.Collections.Generic.List[object]]::new()
    $identityNames = @(
        @{ pattern = '^(manufacturer|manufacturername)$'; metric = 'device.manufacturer' }
        @{ pattern = '^(modelname|model|devicemodel)$'; metric = 'device.model' }
        @{ pattern = '^(softwareversion|firmwareversion|firmware|swversion)$'; metric = 'device.firmware' }
        @{ pattern = '^(hardwareversion|hardware|hwversion)$'; metric = 'device.hardware' }
        @{ pattern = '^operatorname$|^operator_name$'; metric = 'device.operator' }
    )
    foreach ($entry in $identityNames) {
        foreach ($record in @($FieldPathRecords)) {
            if ($record.kind -ne 'scalar') { continue }
            if ([string] $record.name -notmatch $entry.pattern) { continue }
            if (-not (Test-SafeIdentifierValue -Value $record.value)) { continue }
            $endpoint = [string] $record.endpoint
            $phase = if ($EndpointAuth.ContainsKey($endpoint) -and $EndpointAuth[$endpoint] -eq $false) { 'pre_auth' } else { 'post_auth' }
            $value = [string] $record.value
            $ruleId = 'id-' + (New-Slug $entry.metric) + '-' + (Get-StableHash -Text ($endpoint + $record.path) -Length 6)
            $rules.Add([ordered]@{
                id         = $ruleId
                phase      = $phase
                source     = $endpoint
                field      = [string] $record.name
                operator   = 'equals'
                value      = $value
                confidence = 'partial'
                evidence   = 'Non-sensitive value observed in the selected-host HAR response; verify on the device.'
            })
            $candidates.Add([ordered]@{
                metric        = [string] $entry.metric
                endpoint      = $endpoint
                field         = [string] $record.name
                path          = [string] $record.path
                value         = $value
                confidence    = 'partial'
                manual_review = $true
                evidence      = 'Non-sensitive identifier observed; not unique to firmware without further evidence.'
            })
            break
        }
        if ($rules.Count -ge 4) { break }
    }
    if ($rules.Count -eq 0) {
        $rules.Add([ordered]@{
            id         = 'id-unobserved-placeholder'
            phase      = 'pre_auth'
            source     = '<unobserved-endpoint>'
            field      = '<unobserved-identifier>'
            operator   = 'equals'
            value      = '<unobserved-value>'
            confidence = 'unknown'
            evidence   = 'No non-sensitive identifier was observed; replace this placeholder manually.'
        })
    }
    return [ordered]@{ rules = @($rules); candidates = @($candidates) }
}

function New-AuthAnalysis {
    param(
        [string[]] $AllSignals,
        [string[]] $AllFieldNames,
        [object[]] $Sequence,
        [string[]] $LoginEndpointIds,
        [string[]] $SeedEndpointIds,
        $Wrappers
    )
    $signals = @($AllSignals | Select-Object -Unique)
    $names = @($AllFieldNames | Select-Object -Unique)
    $basic = $signals -contains 'http_basic'
    $digest = $signals -contains 'http_digest'
    $token = $signals -contains 'token'
    $session = $signals -contains 'session'
    $form = ($signals -contains 'form_credentials') -and ($signals -contains 'login_post')
    $proprietary = $false
    $proprietaryPatterns = '(loginpd|encrypted|cipher|challenge|nonce|acsrandom|random|seed)'
    if ($names | Where-Object { $_ -match $proprietaryPatterns }) {
        if ($signals -contains 'login_post' -or $signals -contains 'dynamic_value') { $proprietary = $true }
    }

    $fiberhome = $false
    foreach ($wrapper in $Wrappers) {
        $ajaxMethod = ''
        if ($wrapper.query_raw.Contains('ajaxmethod')) { $ajaxMethod = [string] $wrapper.query_raw['ajaxmethod'] }
        $bodyNames = @(Get-PropNames -Object $wrapper.body_obj)
        if ($ajaxMethod -match '^(get_acs_random|do_login)$' -and
            @($bodyNames | Where-Object { $_ -match '^loginpd$' }).Count -gt 0 -and
            @($names | Where-Object { $_ -match '^acsRandom$' }).Count -gt 0 -and
            @($names | Where-Object { $_ -match '^sessionid$' }).Count -gt 0) {
            $fiberhome = $true
        }
    }

    if ($basic) { $flowType = 'http_basic' }
    elseif ($digest) { $flowType = 'http_digest' }
    elseif ($token) { $flowType = 'token' }
    elseif ($fiberhome) { $flowType = 'fiberhome_aes' }
    elseif ($form) { $flowType = 'form' }
    elseif ($proprietary) { $flowType = 'session' }
    elseif ($session) { $flowType = 'session' }
    else { $flowType = 'none' }

    $requiresManual = $false
    if ($proprietary -and -not $fiberhome) { $requiresManual = $true }

    $sessionField = @($names | Where-Object { $_ -match '^(sessionid|session|sid|jsessionid|phpsessid)$' } | Select-Object -First 1)
    $successField = $null
    foreach ($candidate in @('login_result', 'result', 'success', 'status', 'code', 'ret', 'error_code', 'errcode')) {
        $found = @($names | Where-Object { $_ -match ('^' + $candidate + '$') } | Select-Object -First 1)
        if ($found.Count -gt 0) { $successField = [string] $found[0]; break }
    }
    if ([string]::IsNullOrWhiteSpace($successField)) { $successField = '<unobserved-success-field>' }

    $flow = [ordered]@{
        id      = 'observed-auth-flow'
        type    = $flowType
        order   = 1
        success = [ordered]@{ field = $successField }
    }
    if ($LoginEndpointIds.Count -gt 0) {
        $flow['endpoints'] = [ordered]@{ login = [string] $LoginEndpointIds[0] }
    }
    if ($fiberhome -and $SeedEndpointIds.Count -gt 0) {
        if (-not $flow.Contains('endpoints')) { $flow['endpoints'] = [ordered]@{} }
        $flow.endpoints['seed'] = [string] $SeedEndpointIds[0]
        $flow['fields'] = [ordered]@{
            username  = [ordered]@{ source = 'user_input' }
            password  = [ordered]@{ source = 'user_input' }
            sessionid = [ordered]@{ source = 'seed'; field = 'sessionid' }
        }
    }
    if ($session -and $sessionField.Count -gt 0) {
        $flow['session'] = [ordered]@{
            id_field = [string] $sessionField[0]
            store    = $true
            cookie   = $session
        }
    }

    $dynamicNames = @($names | Where-Object { $_ -match '(session|token|csrf|nonce|challenge|random|seed|loginpd|encrypted|cipher)' } | Select-Object -Unique)
    $sequenceNotes = @($Sequence | ForEach-Object {
        ([string] $_.method) + ' ' + ([string] $_.path) + ' (' + ([string] $_.purpose) + ')'
    })
    $notes = @(
        'Authentication was classified from observed requests only; no credentials or secret values were retained.',
        'No encryption algorithm or executable auth handler was inferred.',
        ('Observed transport signals: ' + $(if ($signals.Count -gt 0) { @($signals) -join ', ' } else { 'none' }) + '.'),
        ('Cookie/session evidence observed: ' + $session + '.'),
        ('Login-like endpoint ids: ' + $(if ($LoginEndpointIds.Count -gt 0) { @($LoginEndpointIds) -join ', ' } else { 'none' }) + '.'),
        ('Seed/challenge-like endpoint ids: ' + $(if ($SeedEndpointIds.Count -gt 0) { @($SeedEndpointIds) -join ', ' } else { 'none' }) + '.')
    )
    if ($requiresManual) {
        $notes += 'A proprietary challenge-response pattern was observed; a manual auth handler is required.'
    }
    if ($successField -eq '<unobserved-success-field>') {
        $notes += 'No obvious login success field was observed; success.field is a placeholder for manual review.'
    }

    # This shape must match the existing connector schema's authObservations definition.
    $observations = [ordered]@{
        classification    = if ($requiresManual) { 'proprietary_challenge_response' } else { $flowType }
        proposed_handler  = if ($requiresManual) { 'manual_handler_required' } else { $flowType }
        sequence          = @($sequenceNotes)
        dynamic_values    = @($dynamicNames)
        notes             = @($notes)
        evidence          = 'Observed across ' + $Sequence.Count + ' selected request(s) to the selected host.'
    }
    return [ordered]@{ flow = $flow; observations = $observations; requires_manual = $requiresManual; type = $flowType }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-Warning 'SECURITY: Raw HAR files commonly contain passwords, session cookies, CSRF tokens, PPPoE credentials, phone numbers and public IPs. Keep the HAR private. It is never copied into the draft or evidence outputs.'

$network = Get-NetworkInfo
Write-Host 'Network auto-detection (informational only):'
Write-Host ('  Interface       : ' + $network.interface)
Write-Host ('  Windows IP      : ' + $network.windows_ip)
Write-Host ('  Subnet / prefix : /' + $network.prefix)
Write-Host ('  Default gateway : ' + $(if ($null -ne $network.gateway) { $network.gateway } else { '<not detected>' }))

$targetHost = $null
$targetPort = $null
$targetSource = ''
if (-not [string]::IsNullOrWhiteSpace($Hostname)) {
    $parsedHost = Split-HostAndPort -Value $Hostname
    $targetHost = ([string] $parsedHost.host).Trim().TrimEnd('.').ToLowerInvariant()
    $targetPort = $parsedHost.port
    $targetSource = '-Hostname (manual override)'
    if (-not [string]::IsNullOrWhiteSpace($Gateway)) {
        $parsedGateway = Split-HostAndPort -Value $Gateway
        if ($null -eq $targetPort) { $targetPort = $parsedGateway.port }
    }
} elseif (-not [string]::IsNullOrWhiteSpace($Gateway)) {
    $parsedGateway = Split-HostAndPort -Value $Gateway
    $targetHost = ([string] $parsedGateway.host).Trim().TrimEnd('.').ToLowerInvariant()
    $targetPort = $parsedGateway.port
    $targetSource = '-Gateway (manual override)'
} elseif ($null -ne $network.gateway) {
    $parsedGateway = Split-HostAndPort -Value ([string] $network.gateway)
    $targetHost = ([string] $parsedGateway.host).Trim().TrimEnd('.').ToLowerInvariant()
    $targetPort = $parsedGateway.port
    $targetSource = 'auto-detected IPv4 default gateway'
} else {
    throw 'No target host could be selected. Supply -Gateway or -Hostname explicitly.'
}
if ([string]::IsNullOrWhiteSpace($targetHost)) {
    throw 'The selected target host is empty. Supply a valid -Gateway or -Hostname.'
}
Write-Host ('Target for HAR filtering : ' + $targetHost + $(if ($null -ne $targetPort) { ':' + $targetPort } else { '' }) + ' (' + $targetSource + ')')
Write-Warning 'CAUTION: the default gateway is only a network hint, not proof that it is the ONT UI. Verify the browser address bar. If the UI used a different host or port, rerun with -Hostname/-Gateway. All other HAR traffic is ignored; no scanning or probing is performed.'

if (-not (Test-Path -LiteralPath $HarPath -PathType Leaf)) {
    throw 'HAR file not found. Provide a valid file path with -HarPath.'
}
try {
    $resolvedHar = (Resolve-Path -LiteralPath $HarPath).Path
    $rawHar = [System.IO.File]::ReadAllText($resolvedHar)
} catch {
    throw 'Unable to read the HAR file. No file content was logged.'
}
try {
    $har = $rawHar | ConvertFrom-Json
} catch {
    throw 'The HAR file is not valid JSON. No file content was logged.'
}
$logObject = Get-Prop -Object $har -Name 'log'
if ($null -eq $logObject) { throw "The JSON does not contain a HAR 'log' object." }
$entries = @(To-Array -Value (Get-Prop -Object $logObject -Name 'entries'))
if ($entries.Count -eq 0) { throw 'The HAR log.entries array is empty.' }
$harVersion = [string] (Get-Prop -Object $logObject -Name 'version')
if ($harVersion -notmatch '^1\.[12]') {
    Write-Warning 'The HAR version was not recognized as 1.1 or 1.2; analysis will continue if entries have the expected shape.'
}

$selectedWrappers = [System.Collections.Generic.List[object]]::new()
$ignoredCount = 0
$malformedCount = 0
$entryIndex = 0
foreach ($entry in $entries) {
    $entryIndex++
    $request = Get-Prop -Object $entry -Name 'request'
    $response = Get-Prop -Object $entry -Name 'response'
    if ($null -eq $request -or $null -eq $response) { $malformedCount++; continue }
    $rawUrl = [string] (Get-Prop -Object $request -Name 'url')
    $uri = $null
    if (-not [System.Uri]::TryCreate($rawUrl, [UriKind]::Absolute, [ref] $uri)) { $ignoredCount++; continue }
    if (-not (Test-TargetMatch -Uri $uri -TargetHost $targetHost -TargetPort $targetPort)) { $ignoredCount++; continue }
    $selectedWrappers.Add([ordered]@{
        index     = $entryIndex
        time      = [string] (Get-Prop -Object $entry -Name 'startedDateTime')
        request   = $request
        response  = $response
        uri       = $uri
    })
}
$sorted = @($selectedWrappers | Sort-Object -Property @(
    @{ Expression = { [string] $_.time } },
    @{ Expression = { [int] $_.index } }
))
if ($sorted.Count -eq 0) {
    throw 'No HAR entries matched the selected host. Check the host/port and rerun; no outputs were written.'
}

# Prepare in-memory analysis data. Raw values never leave this process.
foreach ($wrapper in $sorted) {
    $text = Get-ResponseText -Response $wrapper.response
    $content = Get-Prop -Object $wrapper.response -Name 'content'
    $mime = [string] (Get-Prop -Object $content -Name 'mimeType')
    if ([string]::IsNullOrWhiteSpace($mime)) { $mime = [string] (Get-Prop -Object $wrapper.response -Name 'contentType') }
    $parsed = $null
    if (-not [string]::IsNullOrWhiteSpace($text)) {
        try { $parsed = $text | ConvertFrom-Json } catch { $parsed = $null }
    }
    $paths = [System.Collections.Generic.List[object]]::new()
    if ($null -ne $parsed) { Collect-FieldPaths -Value $parsed -Results $paths }
    $values = [System.Collections.Generic.HashSet[string]]::new()
    $fields = [System.Collections.Generic.HashSet[string]]::new()
    if ($null -ne $parsed) { Collect-RawValues -Value $parsed -Values $values -Fields $fields }
    $post = Get-RequestPostData -Request $wrapper.request

    $wrapper['method'] = ([string] (Get-Prop -Object $wrapper.request -Name 'method')).ToUpperInvariant()
    $wrapper['status'] = [int] (Get-Prop -Object $wrapper.response -Name 'status')
    $wrapper['url_info'] = Get-SanitizedUrlInfo -Uri $wrapper.uri
    $wrapper['query_raw'] = Get-RawQueryMap -Uri $wrapper.uri -Request $wrapper.request
    $wrapper['headers'] = @(To-Array -Value (Get-Prop -Object $wrapper.request -Name 'headers'))
    $wrapper['body_obj'] = $post.object
    $wrapper['body_kind'] = [string] $post.kind
    $wrapper['parsed'] = $parsed
    $wrapper['response_text'] = $text
    $wrapper['text_length'] = if ($null -eq $text) { 0 } else { $text.Length }
    $wrapper['format'] = Get-ResponseFormat -Mime $mime -Parsed $parsed
    $wrapper['field_paths'] = @($paths)
    $wrapper['raw_values'] = $values
    $wrapper['raw_fields'] = $fields
    $wrapper['auth_signals'] = @(Get-AuthSignalsForEntry -Wrapper $wrapper)
}

$groups = [ordered]@{}
$usedEndpointIds = @{}
foreach ($wrapper in $sorted) {
    $path = [string] $wrapper.url_info.path
    $queryKey = @($wrapper.url_info.query.Keys | Sort-Object | ForEach-Object {
        ([string] $_) + '=' + ([string] $wrapper.url_info.query[$_])
    }) -join '&'
    $key = [string] $wrapper.method + '|' + $path + '|' + $queryKey
    if (-not $groups.Contains($key)) {
        $baseEndpointId = New-EndpointId -Method ([string] $wrapper.method) -Path $path
        $endpointId = $baseEndpointId
        $suffix = 2
        while ($usedEndpointIds.ContainsKey($endpointId)) {
            $endpointId = $baseEndpointId + '_' + ('{0:d2}' -f $suffix)
            $suffix++
        }
        $usedEndpointIds[$endpointId] = $true
        $groups[$key] = [ordered]@{
            id     = $endpointId
            method = [string] $wrapper.method
            path   = $path
            entries = @()
            values = [System.Collections.Generic.HashSet[string]]::new()
            fields = [System.Collections.Generic.HashSet[string]]::new()
        }
    }
    $groups[$key].entries += $wrapper
    $wrapper['endpoint_id'] = [string] $groups[$key].id
}

$allSignals = @($sorted | ForEach-Object { $_.auth_signals })
$hasTransportAuth = @($allSignals | Where-Object { $_ -in @('http_basic', 'http_digest', 'token', 'session') }).Count -gt 0
$endpointAuth = @{}
foreach ($group in $groups.Values) {
    $preAuthGroup = @($group.entries | Where-Object { $_.auth_signals -contains 'login_post' -or $_.auth_signals -contains 'preauth_helper' }).Count -gt 0
    $authFlag = if (-not $hasTransportAuth) { $false } else { -not $preAuthGroup }
    $group['auth'] = $authFlag
    $endpointAuth[[string] $group.id] = $authFlag
    foreach ($wrapper in $group.entries) { $wrapper['endpoint_auth'] = $authFlag }
}

$sequence = @()
$sequenceOrder = 1
foreach ($wrapper in $sorted) {
    $sequence += [ordered]@{
        order         = $sequenceOrder
        method        = [string] $wrapper.method
        scheme        = [string] $wrapper.url_info.scheme
        host          = '<selected-host>'
        path          = [string] $wrapper.url_info.path
        query_names   = @($wrapper.url_info.query_names)
        status        = [int] $wrapper.status
        format        = [string] $wrapper.format
        endpoint_id   = [string] $wrapper.endpoint_id
        purpose       = Get-Purpose -Wrapper $wrapper
        auth_signals  = @($wrapper.auth_signals)
    }
    $sequenceOrder++
}

$loginEndpointIds = @($groups.Values | Where-Object { @($_.entries | Where-Object { $_.auth_signals -contains 'login_post' }).Count -gt 0 } | ForEach-Object { [string] $_.id })
$seedEndpointIds = @($groups.Values | Where-Object { @($_.entries | Where-Object { $_.auth_signals -contains 'preauth_helper' }).Count -gt 0 } | ForEach-Object { [string] $_.id })

$fieldRecords = @()
foreach ($wrapper in $sorted) {
    foreach ($pathRecord in @($wrapper.field_paths)) {
        $pathRecord['endpoint'] = [string] $wrapper.endpoint_id
        $fieldRecords += $pathRecord
    }
}
$allFieldNames = @($fieldRecords | ForEach-Object { [string] $_.name })
$allFieldNames += @($sorted | ForEach-Object { @(Get-PropNames -Object $_.body_obj) | ForEach-Object { Invoke-RedactText -Text ([string] $_) } })

$authAnalysis = New-AuthAnalysis -AllSignals $allSignals -AllFieldNames $allFieldNames -Sequence $sequence -LoginEndpointIds $loginEndpointIds -SeedEndpointIds $seedEndpointIds -Wrappers $sorted
$identification = New-IdentificationArtifacts -FieldPathRecords $fieldRecords -EndpointAuth $endpointAuth
$extractionArtifacts = New-ExtractionArtifacts -FieldPathRecords $fieldRecords
# No validation rules are invented from field names; units and encodings must
# be confirmed by a reviewer. This matches the Python reference analyzer.
$validation = @()
$capabilities = @(Get-Capabilities -ExtractionRules $extractionArtifacts.rules -FieldPathRecords $fieldRecords)

$manufacturerValue = $null
$modelValue = $null
$firmwareValue = $null
foreach ($candidate in $identification.candidates) {
    if (-not (Test-SafeIdentifierValue -Value $candidate.value)) { continue }
    if ($candidate.metric -eq 'device.manufacturer' -and $null -eq $manufacturerValue) { $manufacturerValue = [string] $candidate.value }
    if ($candidate.metric -eq 'device.model' -and $null -eq $modelValue) { $modelValue = [string] $candidate.value }
    if ($candidate.metric -eq 'device.firmware' -and $null -eq $firmwareValue) { $firmwareValue = [string] $candidate.value }
}
$connectorId = New-ConnectorId -Manufacturer $manufacturerValue -Model $modelValue -FirmwareFamily $FirmwareFamily -Candidates @($identification.candidates) -FieldRecords @($fieldRecords)
$displayIdentity = if ($manufacturerValue -and $modelValue) {
    [regex]::Replace((Invoke-RedactText -Text ($manufacturerValue + ' ' + $modelValue)), '[^A-Za-z0-9 ._-]', '')
} elseif ($modelValue) {
    [regex]::Replace((Invoke-RedactText -Text $modelValue), '[^A-Za-z0-9 ._-]', '')
} else {
    'unidentified'
}
$displayName = 'HAR-derived ONT profile (' + $displayIdentity + ')'
$harLeaf = Split-Path -Path $resolvedHar -Leaf
$safeHarName = 'har-' + (Get-StableHash -Text ([string] $harLeaf) -Length 10) + '.har'
$targetForEvidence = '<selected-host>'

# Build endpoint definitions and observed dependencies in chronological order.
$script:EarlierGroups = @()
$dependencyRecords = [System.Collections.Generic.List[object]]::new()
$endpointDefinitions = @()
$endpointEvidenceMap = @{}
$missingContentCount = 0
foreach ($group in $groups.Values) {
    $definition = New-EndpointDefinition -Group $group -EarlierValues @() -EarlierFieldNames @() -Dependencies $dependencyRecords
    $endpointDefinitions += $definition
    $endpointEvidence = New-EndpointEvidence -Group $group
    $endpointEvidenceMap[[string] $group.id] = $endpointEvidence
    if (@($group.entries | Where-Object { $null -eq $_.parsed }).Count -gt 0) { $missingContentCount++ }
    foreach ($wrapper in $group.entries) {
        if ($null -ne $wrapper.parsed) {
            foreach ($value in $wrapper.raw_values) { $group.values.Add([string] $value) | Out-Null }
            foreach ($field in $wrapper.raw_fields) { $group.fields.Add([string] $field) | Out-Null }
        }
    }
    $script:EarlierGroups += $group
}

$dependencies = [ordered]@{}
foreach ($record in $dependencyRecords) {
    $endpoint = [string] $record.endpoint
    if (-not $dependencies.Contains($endpoint)) { $dependencies[$endpoint] = @() }
    if ($dependencies[$endpoint] -notcontains [string] $record.dependency) {
        $dependencies[$endpoint] += [string] $record.dependency
    }
}

$limitations = @(
    'Draft was generated from a browser HAR only; no live probes, scans, or credential guesses were performed.',
    'Only requests to the explicitly selected host were analyzed; all other HAR traffic was ignored.',
    'Response values were sanitized, so field meanings and units must be confirmed on the real device.',
    'No units, transforms or validation rules were inferred from field names; extraction rules carry only observed paths.',
    'The connector is disabled and must not be enabled until manual review is complete.',
    'Post-generation validation/import requires the repository Python builder; it is not needed to capture or generate this draft.'
)
if ($authAnalysis.requires_manual) {
    $limitations += 'A proprietary challenge-response was observed; a compatible manual auth handler is required before use.'
}
if ($missingContentCount -gt 0) {
    $limitations += ($missingContentCount.ToString() + ' endpoint(s) had no parseable response content in the HAR; recapture with response content if needed.')
}
if ($harVersion -notmatch '^1\.[12]') {
    $limitations += 'HAR version was not recognized as 1.1 or 1.2.'
}
if ($capabilities -contains 'unclassified') {
    $limitations += 'No identity-like field was observed, so capabilities are marked unclassified.'
}

$manualReview = @(
    'Confirm the selected host really is the ONT/router UI; the default gateway was only a hint.',
    'Verify every endpoint path, HTTP method, query parameter, body field and scheme before enabling.',
    'Confirm the authentication flow and select or implement a compatible handler if required.',
    'Verify each candidate field name and nesting actually maps to the proposed universal metric.',
    'Replace any <unobserved-...> placeholder with a value observed on the device before approval.'
)
if ($authAnalysis.requires_manual) {
    $manualReview += 'Document the observed challenge/response sequence for a manual auth handler; no algorithm was assumed.'
}
if ($missingContentCount -gt 0) {
    $manualReview += 'Recapture the HAR with response content enabled for endpoints that had no parseable response.'
}

$evidenceSource = 'Sanitized HAR analysis of traffic to ' + $targetForEvidence + ' (source file: ' + $safeHarName + '); selected host only, verified=false.'
$draft = [ordered]@{
    id                             = $connectorId
    display_name                   = $displayName
    status                         = 'draft'
    enabled                        = $false
    evidence                       = [ordered]@{
        source  = $evidenceSource
        verified = $false
        notes   = @(
            'Generated from HAR request/response structure, never from raw copied content.',
            'Credentials, cookies, tokens, nonces, phone numbers, public IPs, serials, MACs and personal hostnames are redacted.',
            'No endpoint, field, auth method or firmware value was invented; unresolved items are placeholders for manual review.'
        )
    }
    identification                 = [ordered]@{
        confidence = if (@($identification.rules | Where-Object { [string] $_.value -notlike '<unobserved*>' }).Count -gt 0) { 'partial' } else { 'unknown' }
        rules      = @($identification.rules)
    }
    auth                           = [ordered]@{ flows = @($authAnalysis.flow) }
    endpoints                      = @($endpointDefinitions)
    extraction                     = @($extractionArtifacts.rules)
    validation                     = @($validation)
    dependencies                   = $dependencies
    capabilities                   = @($capabilities)
    limitations                    = @($limitations)
    generated_by                   = 'Capture-ONT.ps1 v' + $script:ToolVersion + ' (Windows native, offline HAR analysis)'
    source_har                     = $safeHarName
    requires_manual_auth_handler   = [bool] $authAnalysis.requires_manual
    auth_observations               = $authAnalysis.observations
    identification_candidates       = @($identification.candidates)
    extraction_candidates           = @($extractionArtifacts.candidates)
    metric_mapping_candidates       = @($extractionArtifacts.candidates)
    manual_review                   = @($manualReview)
}

$redactionReport = [ordered]@{
    generated_by          = 'Capture-ONT.ps1 v' + $script:ToolVersion
    source_har            = $safeHarName
    warning               = 'Raw HAR content was never copied into the draft or evidence.'
    har_entries_total     = $entries.Count
    har_entries_selected  = $sorted.Count
    har_entries_ignored   = $ignoredCount
    har_entries_malformed = $malformedCount
    redacted_placeholder_counts = [ordered]@{}
    sensitive_field_names_observed = @($allFieldNames | Where-Object { $null -ne (Get-NamePlaceholder -Name $_) } | Select-Object -Unique)
    outputs               = @(
        (Join-Path $OutputDirectory ($connectorId + '.draft.json')),
        (Join-Path $OutputDirectory ($connectorId + '.evidence\capture-summary.json')),
        (Join-Path $OutputDirectory ($connectorId + '.evidence\redaction-report.json')),
        (Join-Path $OutputDirectory ($connectorId + '.evidence\endpoints\'))
    )
}
foreach ($key in @($script:RedactionCounts.Keys | Sort-Object)) {
    $redactionReport.redacted_placeholder_counts[$key] = [int] $script:RedactionCounts[$key]
}

$captureSummary = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
    tool_version     = $script:ToolVersion
    har_file         = $safeHarName
    har_version      = Invoke-RedactText -Text $harVersion
    connector_id     = $connectorId
    target           = [ordered]@{
        host_for_evidence = $targetForEvidence
        port              = $targetPort
        selection_source  = $targetSource
        note              = 'The default gateway is only a network hint; the selected host was used as a strict URL filter.'
    }
    network          = [ordered]@{
        interface  = Invoke-RedactText -Text ([string] $network.interface)
        windows_ip = Invoke-RedactText -Text ([string] $network.windows_ip)
        prefix     = Invoke-RedactText -Text ([string] $network.prefix)
        gateway    = if ($null -eq $network.gateway) { '<not detected>' } else { Invoke-RedactText -Text ([string] $network.gateway) }
    }
    totals           = [ordered]@{
        entries_total    = $entries.Count
        entries_selected = $sorted.Count
        entries_ignored  = $ignoredCount
        entries_malformed = $malformedCount
        endpoints        = $groups.Count
    }
    call_sequence    = @($sequence)
    auth_observations = $authAnalysis.observations
    capabilities     = @($capabilities)
    limitations      = @($limitations)
    note             = 'All paths, field names and structures are sanitized; no raw HAR content, credentials, secrets or personal identifiers are stored.'
}

$outputDirectoryFull = [System.IO.Path]::GetFullPath($OutputDirectory)
$evidenceDirectory = Join-Path $outputDirectoryFull ($connectorId + '.evidence')
$endpointsDirectory = Join-Path $evidenceDirectory 'endpoints'
Ensure-Directory -Path $outputDirectoryFull
Ensure-Directory -Path $evidenceDirectory
Ensure-Directory -Path $endpointsDirectory

foreach ($endpointId in @($endpointEvidenceMap.Keys)) {
    $safeEndpointFile = [regex]::Replace([string] $endpointId, '[^A-Za-z0-9_.-]', '_') + '.json'
    Write-JsonFile -Path (Join-Path $endpointsDirectory $safeEndpointFile) -Data $endpointEvidenceMap[$endpointId]
}
Write-JsonFile -Path (Join-Path $evidenceDirectory 'capture-summary.json') -Data $captureSummary
Write-JsonFile -Path (Join-Path $evidenceDirectory 'redaction-report.json') -Data $redactionReport
$draftPath = Join-Path $outputDirectoryFull ($connectorId + '.draft.json')
Write-JsonFile -Path $draftPath -Data $draft

Write-Host ''
Write-Host ('Selected HAR entries : ' + $sorted.Count + ' of ' + $entries.Count + ' (' + $ignoredCount + ' ignored, ' + $malformedCount + ' malformed)')
Write-Host ('Endpoints derived    : ' + $groups.Count)
Write-Host ('Auth classification  : ' + $authAnalysis.type + $(if ($authAnalysis.requires_manual) { ' (manual auth handler required)' } else { '' }))
Write-Host ('Draft                : ' + $draftPath)
Write-Host ('Evidence directory   : ' + $evidenceDirectory)
Write-Warning 'Review the draft and manual_review items before validation/import. Python is only needed later for: python -m tools.connector_builder validate-draft ... and import-draft --approve ...'

