# Importação e integridade — Nery Mizuki Kit v1

**Estado:** kit v1 importado para `assets/mascot/kit-v1/` e versionado no repositório. Este documento descreve como validar ou reinstalar a mesma versão; não é necessário executar o importador para usar os arquivos já publicados.

## Conteúdo

- **Prancha aprovada:** `assets/mascot/kit-v1/source/approved-character-sheet.png` (1536 × 1024 px).
- **Oito recortes originais** em `source/original-crops/`.
- **Oito poses/expressões com canal alfa**, em PNG e WebP: retrato, corpo inteiro, feliz, pensando, dormindo, programando, com café e com gato.
- **Avatares quadrados** em 128, 256 e 512 px, PNG e WebP.
- **Prévia:** `assets/mascot/kit-v1/preview/nery-kit-preview.png`; é uma composição de ilustrações, não screenshot real da aplicação.
- **Manifesto:** `assets/mascot/kit-v1/manifest.json`; registra tamanho, origem, processos e limitações.
- **Guia:** [KIT.md](KIT.md).

Os recortes mantêm suas resoluções nativas. Em especial, o corpo inteiro tem somente **195 × 403 px**; a prancha de 1536 × 1024 px **não** significa que cada pose tenha essa resolução. Os avatares usam recortes e redimensionamentos do retrato fornecido, não são pinturas originais novas.

## Verificação do ZIP original (opcional)

O ZIP **não é necessário para executar a aplicação**. Ele foi recebido separadamente e mantido fora do repositório.

- Nome: `nery-mizuki-kit-v1.zip`
- SHA-256 esperado: `ec4ee4a77f1253d9b074e55ea8d2f0c138ca58fad34f6fa75f0fd35a846cb657`
- 35 entradas no ZIP; 34 arquivos instalados. O script de instalação embutido no ZIP **não é executado**.

Em um checkout Mnerys, para verificar uma cópia local do ZIP:

```bash
python3 scripts/import_nery_kit.py /path/to/nery-mizuki-kit-v1.zip
```

Com os assets versionados e intactos, o resultado esperado é `new_files: 0` e `installed: false`. A opção `--apply` só é necessária em um checkout sem esses arquivos; o importador confere o hash, bloqueia sobrescrita de conteúdo diferente e rejeita caminhos não autorizados.

## Uso indicado

| Caso | Asset (relativo a `assets/mascot/kit-v1/`) |
| --- | --- |
| Boas-vindas, sucesso | `transparent/nery-happy.webp` |
| Processando, ajuda | `transparent/nery-thinking.webp` |
| Ferramentas, programação | `transparent/nery-coding.webp` |
| Pausa | `transparent/nery-coffee.webp` |
| Descanso | `transparent/nery-sleeping.webp` |
| Integrações | `transparent/nery-with-cat.webp` |
| Apresentação da personagem | `transparent/nery-fullbody.webp` |
| Avatar alternativo | `avatars/nery-avatar-256.webp` |

**Separação de marca:** Nery é mascote, não logotipo. Não substituir o M geométrico nos ícones do app, favicon, PWA ou wordmarks.

## Segurança, licenciamento e qualidade

As imagens foram inspecionadas e verificadas quanto a formato, tamanho e transparência. Não foram detectados metadados EXIF nos arquivos examinados. A remoção de fundo foi semiautomática; **podem existir imperfeições nos contornos**, especialmente em cabelos, roupa escura e bordas de recortes. Não alegar transparência perfeita ou arte original em alta resolução.

A arte é fornecida e aprovada para o projeto, mas as condições específicas de redistribuição devem ser decididas/documentadas pelo titular. **A AGPL-3.0 do código não define automaticamente a licença das ilustrações.** Antes de publicar um pacote independente de imagens, explicitar sua licença.

Não colocar ZIPs privados, logs, arquivos de trabalho ou dados internos no Git. As imagens do kit são arte ilustrativa, não capturas da aplicação.
