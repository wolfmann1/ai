function New-Secret {
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

New-Secret   # WEBUI_SECRET_KEY
New-Secret   # LANGFUSE_NEXTAUTH_SECRET
New-Secret   # LANGFUSE_SALT
New-Secret   # LANGFUSE_ENCRYPTION_KEY