# Nery Mizuki — kit de assets v1.0

**Mnerys — Built to be yours.**

Este pacote organiza as **artes aprovadas da Nery** em arquivos utilizáveis para frontend, onboarding, documentação e estados de interface. A Nery **não é** o logotipo: o logo do Mnerys continua sendo o **M geométrico neutro**.

## Conteúdo

- `source/approved-character-sheet.png`: prancha aprovada original (1536×1024).
- `source/original-crops/`: oito recortes originais, com fundo, em resolução nativa.
- `transparent/`: oito artes com transparência real em **PNG RGBA** e **WebP RGBA**, incluindo retrato, corpo inteiro, feliz, pensando, dormindo, programando, café e gato.
- `avatars/`: recortes quadrados 128, 256 e 512 px em PNG/WebP, **reduzidos** do retrato original de 745×905; não são upscales.
- `preview/nery-kit-preview.png`: composição ilustrativa dos arquivos finais (não é screenshot do app).
- `manifest.json`: inventário, medidas, origem e limitações.

## Uso sugerido no Mnerys

| Estado | Asset |
| --- | --- |
| Welcome / apresentação | `transparent/nery-happy.webp` |
| Pensando / aguardando | `transparent/nery-thinking.webp` |
| Trabalhando / tool call | `transparent/nery-coding.webp` |
| Modo descanso / sem tarefas | `transparent/nery-sleeping.webp` |
| Pausa / onboarding | `transparent/nery-coffee.webp` |
| Integrações simpáticas | `transparent/nery-with-cat.webp` |
| Referência de personagem | `transparent/nery-fullbody.png` |
| Avatar / perfil | `avatars/nery-avatar-256.webp` |

## Transparência e qualidade

Os fundos foram removidos a partir dos **pixels da arte aprovada**. Os recortes não foram redesenhados ou artificialmente ampliados: a resolução individual de cada pose continua igual à original. Transparência verificada no canal alpha. A arte de corpo inteiro tem largura nativa de apenas **195 px**; usá-la em um painel de 1024 px degradaria a nitidez. Os originais estão preservados para futuras revisões de bordas ou nova exportação.

**Não chamar upscale de original de alta resolução.** Para corpo inteiro em alta definição ou expressões extras verdadeiramente grandes, é necessária uma arte original adicional.

## Licenciamento e privacidade

**© 2026 Yuri Cunha. Todos os direitos reservados.** A licença oficial das ilustrações é [Nery Mizuki — Proprietary Mascot Asset License](../../assets/mascot/LICENSE.md). É permitido instalar e exibir a Nery dentro de uma instalação autorizada do Mnerys. Redistribuir, comercializar, modificar para outros usos ou reutilizar a arte fora do Mnerys depende de autorização expressa e escrita do titular. A licença **AGPL-3.0 do código não se aplica a essas artes proprietárias**; forks que redistribuam o código devem excluir/substituir os assets da Nery, salvo autorização. Nenhum IP interno, domínio privado, token ou credencial está incluído.
