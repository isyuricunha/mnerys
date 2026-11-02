# Importação do kit oficial Nery Mizuki v1

**Estado:** importador pronto, pacote binário **ainda não transferido** para este checkout. Não apresentar os arquivos de `kit-v1/` como publicados até confirmar sua presença e o commit.

O kit foi preparado a partir de uma prancha aprovada de 1536 × 1024 px e reúne oito recortes, oito variações transparentes (PNG/WebP), três tamanhos de avatar (PNG/WebP), previews, originais e manifesto. A resolução de cada ilustração individual varia: por exemplo, corpo inteiro tem apenas 195 × 403 px. Os avatares são redimensionamentos menores; não são pinturas originais em alta resolução.

## Integridade

- Arquivo de entrada: `nery-mizuki-kit-v1.zip`
- SHA-256: `ec4ee4a77f1253d9b074e55ea8d2f0c138ca58fad34f6fa75f0fd35a846cb657`
- 35 entradas no ZIP; 34 arquivos aprovados para instalação; o instalador anexado ao ZIP não é executado.
- Origem: arte fornecida/aprovada pelo proprietário do Mnerys, com recortes e transparência extraídos dos pixels disponíveis. Não é uma nova pintura em 4K.
- Direitos de redistribuição das artes devem ser documentados separadamente dos direitos do código AGPL.

## Instalar depois que o ZIP estiver no servidor

Coloque o ZIP em um caminho **privado**, fora do checkout Git; não coloque arquivos temporários ou o ZIP bruto na documentação pública. Rode na raiz do checkout:

```bash
python3 scripts/import_nery_kit.py /path/to/nery-mizuki-kit-v1.zip
python3 scripts/import_nery_kit.py /path/to/nery-mizuki-kit-v1.zip --apply
```

O primeiro comando valida o hash e apresenta uma prévia sem escrita. O segundo instala os PNGs/WebPs em `assets/mascot/kit-v1/` e o README do kit em `docs/mascot/KIT.md`. O processo preserva os arquivos já existentes, rejeita ZIPs diferentes e é idempotente para cópias iguais.

Depois, revise as transparências e as bordas (principalmente as meias do corpo inteiro), execute os testes, inspecione os novos arquivos e o diff em busca de dados internos, documente a licença de redistribuição e só então faça commit/push nos três remotes. O símbolo geométrico M e os temas antigos do Odysseus não devem ser alterados.

## Guia de uso, após a instalação

| Situação | Arte sugerida |
| --- | --- |
| Boas-vindas / sucesso | `transparent/nery-happy.webp` |
| Pensando / ajuda | `transparent/nery-thinking.webp` |
| Ferramentas / programação | `transparent/nery-coding.webp` |
| Descanso | `transparent/nery-sleeping.webp` |
| Pausa | `transparent/nery-coffee.webp` |
| Integrações | `transparent/nery-with-cat.webp` |
| Apresentação do personagem | `transparent/nery-fullbody.webp` |
| Avatar | `avatars/nery-avatar-256.webp` |

A Nery é uma **mascote**, não o logo do produto. Não substituir favicon, PWA e wordmarks pela personagem.
