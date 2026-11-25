/* SIR88 V27: STM32F103C8 9N1, independent USART1 TX and RX BRR.
 * V26 with an optional leading command word: public volatile word_0.
 * word_0==0 (default): commands are sent as 3 words, V26-compatible.
 * word_0>0: commands on the bus become 4 words - word_0 first, then the
 * 27-bit packed triple (w0,w1,w2). Host writes word_0 once via SWD while
 * the scan is idle, before starting passes.
 * Six automatic passes: 2-word forward and reverse, then V23 A-RX
 * triples forward and reverse, then V23 B-RX triples forward and reverse.
 * Host streams 27-bit packed triples through SWD in 512-command batches.
 * V22/V23/V24/V25 source files, ABI and scan results are not modified.
 * USART1 BRR=64 by default; independently configurable for each scan pass.
 * DE-off wait, response encoding, command queue and all 6 passes unchanged.
 * MAX485 5 V: PA9->DI, PA10<-RO, PB0->DE+/RE tied.
 * IMPORTANT: bench-test with actuators disconnected; unknown commands may switch relays.
 */
#include "stm32f1xx_hal.h"
#include <stdint.h>

#define CPU_HZ             48000000UL
#define CYC_PER_US         48UL
#define SCAN_MAGIC         0x91B02691UL
#define BRR_DEFAULT        64U
#define BRR_MIN            16U
#define BRR_MAX            65535U
#define V17_MAGIC          0x91B01791UL
#define TWO_TOTAL          262144UL
#define A_RX_TOTAL         80722UL
#define B_RX_TOTAL         256733UL
#define CMDS_PER_BATCH     512UL
#define STAGE_COUNT        6U
#define SCAN_BUF_SIZE      8192UL
#define MAX_SCAN_ENTRY     31UL
#define WAIT_START_CYC     (84UL*CYC_PER_US)  /* guaranteed 80..86 us after DE-off */
#define GAP_CYC            (30UL*CYC_PER_US)
#define POST_CYC           (40UL*CYC_PER_US)
#define ERR_MASK           (USART_SR_PE|USART_SR_FE|USART_SR_NE|USART_SR_ORE)
#define UART_INT_MASK      (USART_SR_RXNE|ERR_MASK)
#define LEGACY_WORDS       16U
#define LEGACY_GOOD        32U
#define LEGACY_BAD         16U
#define LEGACY_CAP         16U

/* States 0 idle, 1 running, 2 block ready / STOPPED, 3 finished, 4 hardware error. */
#define STATE_IDLE 0U
#define STATE_RUN  1U
#define STATE_BLOCK 2U
#define STATE_DONE 3U
#define STATE_ERROR 4U
#define STATE_NEED_CMDS 5U
#define CAP_NONE   0U
#define CAP_SCAN   1U
#define CAP_LEGACY 2U

/* Legacy record MUST remain exactly 304 bytes. */
typedef struct {
    uint32_t sequence,ticket,mode,tx_len,good_total,bad_total,error_or,status;
    uint32_t rx_window_us,time_ms,uart_brr,reserved;
    uint16_t tx_words[16],rx_good[32],rx_bad[16],rx_good_us[32],rx_bad_us[16],rx_bad_flags[16];
} NineRecord;
typedef char AssertLegacy304[(sizeof(NineRecord)==304U)?1:-1];

/* Public legacy mailbox: existing sir88_exhaustive_v18.py ABI. */
volatile uint32_t swd_fw_magic=V17_MAGIC;
volatile uint32_t swd_mailbox_mode=2U,swd_mailbox_len=0U;
volatile uint32_t swd_mailbox_ticket=0U,swd_mailbox_done=0U,swd_mailbox_busy=0U;
volatile uint32_t swd_mailbox_words[LEGACY_WORDS]={0U};
volatile uint32_t swd_log_head=0U,swd_record_size=sizeof(NineRecord);
volatile uint32_t swd_record_capacity=LEGACY_CAP,swd_last_status=0U,swd_uart_brr=0U;
volatile NineRecord swd_log[LEGACY_CAP];

/* Public scan control and immutable, host-acknowledged data block.
 * scan_protocol is deliberately read in main() to keep its linker map symbol. */
volatile uint32_t scan_magic=SCAN_MAGIC,scan_protocol=26U;
volatile uint32_t scan_buf_capacity=SCAN_BUF_SIZE,scan_total=TWO_TOTAL;
volatile uint32_t scan_cmd_capacity=CMDS_PER_BATCH;
volatile uint32_t scan_cmd_req_seq=0U,scan_cmd_ready_seq=0U;
volatile uint32_t scan_cmd_req_start=0U;
volatile uint32_t scan_cmd_start=0U,scan_cmd_count=0U,scan_cmd_crc32=0U,scan_cmd_stage=0U;
volatile uint32_t scan_cmd_buffer[CMDS_PER_BATCH];
volatile uint32_t scan_request_seq=0U,scan_request_done=0U;
volatile uint32_t scan_request_phase=1U,scan_request_start=0U;
volatile uint32_t scan_state=STATE_IDLE,scan_phase=0U,scan_next_order=0U;
volatile uint32_t scan_chunk_seq=0U,scan_ack_seq=0U;
volatile uint32_t scan_chunk_start=0U,scan_chunk_count=0U;
volatile uint32_t scan_chunk_bytes=0U,scan_chunk_crc32=0U;
volatile uint32_t scan_error=0U,scan_brr=0U;
/* V26 SWD configuration. Host writes requested BRR only BETWEEN stages.
 * Values are latched once on ScanPass entry; cannot change mid-pass.
 * 64 / 64 is wire-timing identical to V25 (no BRR switch needed).
 * Effective values are public for debugging and are logged by the host.
 */
volatile uint32_t scan_tx_brr_request=BRR_DEFAULT;
volatile uint32_t scan_rx_brr_request=BRR_DEFAULT;
volatile uint32_t scan_tx_brr_active=BRR_DEFAULT;
volatile uint32_t scan_rx_brr_active=BRR_DEFAULT;
/* V27: optional leading 9-bit command word. 0 (default) = 3-word commands,
 * fully V26-compatible. Non-zero = 4-word commands: word_0 first, then the
 * packed triple. Written by the host once between scans; not latched.
 */
volatile uint32_t word_0=0U;
volatile uint32_t scan_fw_variant=27U;
static uint32_t uart_current_brr=BRR_DEFAULT;
static uint32_t rx_frame_cycles=11U*BRR_DEFAULT;
static uint32_t first_word_wait_cycles=22U*CYC_PER_US;
static uint32_t silence_gap_cycles=GAP_CYC;
static uint32_t tx_wait_cycles=48000U;

static uint32_t BrrValid(uint32_t v) {return (v>=BRR_MIN && v<=BRR_MAX);}
/* Caller ensures no TX is in progress and receiver has been disabled.
 * USART is briefly disabled as in the user-supplied split-BRR prototype.
 */
static void SetUartBrrIfDifferent(uint32_t brr) {
    uint32_t cr1;
    if(uart_current_brr==brr) return;
    cr1=USART1->CR1;
    USART1->CR1=cr1 & ~USART_CR1_UE;
    USART1->BRR=brr;
    USART1->CR1=cr1;
    uart_current_brr=brr;
}
volatile uint8_t scan_buffer[SCAN_BUF_SIZE];

/* Interrupt-produced RX state for current request. */
static volatile uint32_t rx_kind=CAP_NONE;
static volatile uint32_t rx_count=0U,rx_last_cycle=0U,rx_first_word_cycle=0U;
static volatile uint32_t rx_first_edge=0U,rx_edge_found=0U;
static volatile uint16_t rx_words[8];
static volatile uint8_t rx_flags[8];
static volatile NineRecord *legacy_record=0;
static volatile uint32_t legacy_tx_start=0U;
static uint32_t block_used=0U,block_records=0U,block_start=0U,block_seq=0U;

void Error_Handler(void);
static void DelayCycles(uint32_t cycles) {
    uint32_t t=DWT->CYCCNT;
    while((uint32_t)(DWT->CYCCNT-t)<cycles) { __NOP(); }
}
static void DelayUs(uint32_t us) { DelayCycles(us*CYC_PER_US); }
static void Clock48(void) {
    RCC_OscInitTypeDef o={0}; RCC_ClkInitTypeDef c={0};
    HAL_RCC_DeInit();
    o.OscillatorType=RCC_OSCILLATORTYPE_HSE;
    o.HSEState=RCC_HSE_ON;
    o.HSEPredivValue=RCC_HSE_PREDIV_DIV1;
    o.HSIState=RCC_HSI_ON;
    o.PLL.PLLState=RCC_PLL_ON;
    o.PLL.PLLSource=RCC_PLLSOURCE_HSE;
    o.PLL.PLLMUL=RCC_PLL_MUL6;
    if(HAL_RCC_OscConfig(&o)!=HAL_OK) Error_Handler();
    c.ClockType=RCC_CLOCKTYPE_SYSCLK|RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
    c.SYSCLKSource=RCC_SYSCLKSOURCE_PLLCLK;
    c.AHBCLKDivider=RCC_SYSCLK_DIV1;
    c.APB1CLKDivider=RCC_HCLK_DIV2;
    c.APB2CLKDivider=RCC_HCLK_DIV1;
    if(HAL_RCC_ClockConfig(&c,FLASH_LATENCY_1)!=HAL_OK) Error_Handler();
    if(HAL_RCC_GetPCLK2Freq()!=CPU_HZ) Error_Handler();
}
static void Pins(void) {
    GPIO_InitTypeDef g={0};
    __HAL_RCC_GPIOA_CLK_ENABLE();__HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOC_CLK_ENABLE();__HAL_RCC_AFIO_CLK_ENABLE();
    __HAL_RCC_USART1_CLK_ENABLE();
    GPIOB->BRR=GPIO_PIN_0;
    g.Pin=GPIO_PIN_0;g.Mode=GPIO_MODE_OUTPUT_PP;g.Speed=GPIO_SPEED_FREQ_HIGH;g.Pull=GPIO_NOPULL;
    HAL_GPIO_Init(GPIOB,&g);
    g.Pin=GPIO_PIN_9;g.Mode=GPIO_MODE_AF_PP;g.Pull=GPIO_NOPULL;
    HAL_GPIO_Init(GPIOA,&g);
    g.Pin=GPIO_PIN_10;g.Mode=GPIO_MODE_INPUT;g.Pull=GPIO_NOPULL;
    HAL_GPIO_Init(GPIOA,&g);
    GPIOC->BSRR=GPIO_PIN_13;
    g.Pin=GPIO_PIN_13;g.Mode=GPIO_MODE_OUTPUT_PP;g.Speed=GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOC,&g);
    /* EXTI10 on PA10: timestamp FIRST falling edge (physical start of RX). */
    AFIO->EXTICR[2] &= ~(0xFU<<8);       /* EXTI10 routed to Port A */
    EXTI->IMR &= ~(1UL<<10);
    EXTI->RTSR &= ~(1UL<<10);
    EXTI->FTSR |= (1UL<<10);
    EXTI->PR = (1UL<<10);
    NVIC_SetPriority(EXTI15_10_IRQn,1U);
    NVIC_EnableIRQ(EXTI15_10_IRQn);
}
static void UartInit(void) {
    USART1->CR1=0U;
    USART1->CR2=0U;                  /* exactly 1 stop bit */
    USART1->CR3=USART_CR3_EIE;
    USART1->BRR=BRR_DEFAULT;        /* APB2 48 MHz /64 = 750000 */
    uart_current_brr=BRR_DEFAULT;
    (void)USART1->SR;(void)USART1->DR;
    USART1->CR1=USART_CR1_UE|USART_CR1_TE|USART_CR1_M|USART_CR1_RXNEIE;
    NVIC_SetPriority(USART1_IRQn,0U);
    NVIC_EnableIRQ(USART1_IRQn);
    swd_uart_brr=USART1->BRR;
    scan_brr=USART1->BRR;
}
void EXTI15_10_IRQHandler(void) {
    if(EXTI->PR & (1UL<<10)) {
        EXTI->PR = (1UL<<10);
        if(rx_kind==CAP_SCAN && !rx_edge_found) {
            rx_first_edge=DWT->CYCCNT;
            rx_edge_found=1U;
            EXTI->IMR &= ~(1UL<<10);
        }
    }
}
void USART1_IRQHandler(void) {
    uint32_t sr=USART1->SR;
    if(sr & UART_INT_MASK) {
        uint16_t w=(uint16_t)(USART1->DR & 0x1FFU);
        uint8_t f=(uint8_t)(sr & ERR_MASK);
        uint32_t now=DWT->CYCCNT;
        if(rx_kind==CAP_SCAN) {
            uint32_t i=rx_count;
            if(i<8U) {rx_words[i]=w;rx_flags[i]=f;}
            if(i==0U) rx_first_word_cycle=now;
            if(i<0xFFFFFFFFUL) rx_count=i+1U;
            rx_last_cycle=now;
        } else if(rx_kind==CAP_LEGACY && legacy_record) {
            volatile NineRecord *r=legacy_record;
            uint16_t us=(uint16_t)((uint32_t)(now-legacy_tx_start)/CYC_PER_US);
            r->error_or |= f;
            if(f) {
                uint32_t b=r->bad_total++;
                if(b<LEGACY_BAD){r->rx_bad[b]=w;r->rx_bad_flags[b]=f;r->rx_bad_us[b]=us;}
            } else {
                uint32_t g=r->good_total++;
                if(g<LEGACY_GOOD){r->rx_good[g]=w;r->rx_good_us[g]=us;}
            }
        }
    }
}
/* TX only. uart RX disabled through entire transmission and for 1us after DE-off.
 * Returns DE-off DWT cycle, or 0 on timeout. Call with n 1..16. */
static uint32_t SendWords(const uint16_t *words,uint32_t n) {
    uint32_t i,t;
    rx_kind=CAP_NONE;
    EXTI->IMR &= ~(1UL<<10);
    USART1->CR1 &= ~(USART_CR1_RE|USART_CR1_RXNEIE);
    SetUartBrrIfDifferent(scan_tx_brr_active);
    (void)USART1->SR;(void)USART1->DR;
    GPIOB->BSRR=GPIO_PIN_0;              /* DE=1, /RE=1 */
    DelayUs(2U);
    for(i=0U;i<n;i++) {
        t=DWT->CYCCNT;
        while(!(USART1->SR & USART_SR_TXE)) {
            if((uint32_t)(DWT->CYCCNT-t)>tx_wait_cycles) goto fail;
        }
        USART1->DR=words[i]&0x1FFU;
    }
    t=DWT->CYCCNT;
    while(!(USART1->SR & USART_SR_TC)) {
        if((uint32_t)(DWT->CYCCNT-t)>tx_wait_cycles) goto fail;
    }
    GPIOB->BRR=GPIO_PIN_0;               /* DE=0, /RE=0 */
    t=DWT->CYCCNT;
    /* TX is complete; switch only RX baud while receiver is still disabled.
     * The DE-off timestamp remains the timing reference for RX records.
     */
    SetUartBrrIfDifferent(scan_rx_brr_active);
    DelayUs(1U);                          /* MAX485 RO settle */
    (void)USART1->SR;(void)USART1->DR;
    rx_kind=CAP_SCAN;                     /* will be overridden for legacy */
    USART1->CR1 |= USART_CR1_RE|USART_CR1_RXNEIE;
    EXTI->PR=(1UL<<10);
    EXTI->IMR|=(1UL<<10);
    return t;
fail:
    GPIOB->BRR=GPIO_PIN_0;
    rx_kind=CAP_NONE;
    return 0U;
}
/* CRC-32/IEEE computed when publishing a frozen chunk. */
static uint32_t Crc32(const volatile uint8_t *buf,uint32_t len) {
    uint32_t crc=0xFFFFFFFFUL, i,k;
    for(i=0;i<len;i++) {
        crc^=buf[i];
        for(k=0;k<8U;k++) crc=(crc>>1)^((crc&1U)?0xEDB88320UL:0U);
    }
    return ~crc;
}
static void Put8(uint8_t v) {scan_buffer[block_used++]=v;}
static void Put16(uint16_t v) {Put8((uint8_t)v);Put8((uint8_t)(v>>8));}
static void Put32(uint32_t v) {Put16((uint16_t)v);Put16((uint16_t)(v>>16));}
static void AddResult(uint32_t count,uint16_t delay_cycles,uint8_t approx_start) {
    uint32_t i,k=(count<8U)?count:8U;
    if(count==0U) Put8(0x00U);
    else {
        Put8((uint8_t)(0x80U | (approx_start?0x20U:0U) | k));
        Put32(count);
        Put16(delay_cycles);
        for(i=0U;i<k;i++) {
            Put16(rx_words[i]);
            Put8(rx_flags[i]);
        }
    }
    block_records++;
}
/* Total record counts for all six stages. */
static uint32_t StageTotal(uint32_t phase) {
    if(phase==1U || phase==2U) return TWO_TOTAL;
    if(phase==3U || phase==4U) return A_RX_TOTAL;
    if(phase==5U || phase==6U) return B_RX_TOTAL;
    return 0U;
}
static uint32_t EncodeIndex(uint32_t phase,uint32_t order,uint16_t *w) {
    uint32_t flat,packed;
    if(phase<=2U) {
        flat=(phase==1U)?order:(TWO_TOTAL-1U-order);
        w[0]=(uint16_t)(flat>>9);
        w[1]=(uint16_t)(flat&511U);
        return 2U;
    }
    if(scan_cmd_stage!=phase || order<scan_cmd_start ||
       order-scan_cmd_start>=scan_cmd_count) return 0U;
    packed=scan_cmd_buffer[order-scan_cmd_start];
    if(packed & 0xF8000000UL) return 0U; /* 27 bits used */
    w[0]=(uint16_t)((packed>>18)&511UL);
    w[1]=(uint16_t)((packed>>9)&511UL);
    w[2]=(uint16_t)(packed&511UL);
    if(word_0!=0U) { /* V27: prepend the leading word -> 4-word command */
        w[3]=w[2]; w[2]=w[1]; w[1]=w[0];
        w[0]=(uint16_t)(word_0&0x1FFUL);
        return 4U;
    }
    return 3U;
}
/* RX deadline is DE-off +84us without a falling edge.
 * If edge has occurred before deadline, allow at least 22us (or one
 * RX frame + 7us) for the first decoded word. The post-word silence gap
 * is at least 30us (or one RX frame + 7us). Post-RX pause remains 40us.
 * A lone edge without decoded UART words still yields RX0.
 */
static uint32_t ScanOne(uint32_t phase,uint32_t order) {
    uint16_t w[4];uint32_t t,first,now,last,count,n;
    uint16_t delay=0U;uint8_t approx=0U;
    n=EncodeIndex(phase,order,w);
    if(n==0U) return 3U;
    rx_count=0U;rx_edge_found=0U;rx_first_edge=0U;rx_first_word_cycle=0U;rx_last_cycle=0U;
    t=SendWords(w,n);
    if(!t) return 1U;
    first=t;
    for(;;) {
        now=DWT->CYCCNT;
        if(!rx_edge_found && rx_count>0U) {
            /* EXTI missed start, but USART delivered a word: fallback timing
               is inferred from nominal frame length and explicitly flagged. */
            rx_first_edge=rx_first_word_cycle-rx_frame_cycles;
            rx_edge_found=2U;approx=1U;
        }
        if(rx_count>0U) {
            last=rx_last_cycle;
            if((uint32_t)(now-last)>=silence_gap_cycles) break;
        } else if(!rx_edge_found) {
            if((uint32_t)(now-first)>=WAIT_START_CYC) break;
        } else if((uint32_t)(now-rx_first_edge)>=first_word_wait_cycles &&
                  (uint32_t)(now-first)>=WAIT_START_CYC) {
            /* Physical falling edge, but not a decoded word: RX=0. */
            break;
        }
    }
    /* Stop capture at 30 us of silence, then copy saved first eight events. */
    EXTI->IMR &= ~(1UL<<10);
    USART1->CR1 &= ~(USART_CR1_RE|USART_CR1_RXNEIE);
    rx_kind=CAP_NONE;
    count=rx_count;
    if(rx_edge_found) {
        uint32_t diff=(uint32_t)(rx_first_edge-first);
        delay=(uint16_t)(diff>65535UL?65535UL:diff);
    }
    AddResult(count,delay,approx);
    if(count>0U) DelayCycles(POST_CYC);
    return 0U;
}
static void PublishChunk(void) {
    if(block_records==0U) return;
    scan_chunk_start=block_start;
    scan_chunk_count=block_records;
    scan_chunk_bytes=block_used;
    scan_chunk_crc32=Crc32(scan_buffer,block_used);
    scan_chunk_seq=++block_seq;
    __DMB();scan_state=STATE_BLOCK;__DMB();
    /* PC dumps immutable data, fsyncs to disk and acknowledges sequence. */
    while(scan_ack_seq!=scan_chunk_seq) {__NOP();}
    scan_state=STATE_RUN;
    block_used=0U;block_records=0U;
    block_start=scan_next_order;
}
static uint32_t NeedHostCommands(uint32_t phase,uint32_t order,uint32_t total) {
    uint32_t n,i;
    if(scan_cmd_stage==phase && order>=scan_cmd_start &&
       order-scan_cmd_start<scan_cmd_count) return 0U;
    scan_cmd_req_start=order;
    scan_cmd_req_seq++;
    if(scan_cmd_req_seq==0U) scan_cmd_req_seq=1U;
    scan_state=STATE_NEED_CMDS;
    __DMB();
    while(scan_cmd_ready_seq!=scan_cmd_req_seq) {__NOP();}
    __DMB();
    n=scan_cmd_count;
    if(scan_cmd_stage!=phase || scan_cmd_start!=order ||
       n==0U || n>CMDS_PER_BATCH || n>total-order) return 6U;
    if(Crc32((const volatile uint8_t*)scan_cmd_buffer,n*4U)!=scan_cmd_crc32) return 7U;
    for(i=0U;i<n;i++) if(scan_cmd_buffer[i] & 0xF8000000UL) return 8U;
    scan_state=STATE_RUN;
    return 0U;
}
static void ScanPass(uint32_t phase,uint32_t start) {
    uint32_t order,err=0U;
    uint32_t total=StageTotal(phase);
    uint32_t tx=scan_tx_brr_request,rx=scan_rx_brr_request;
    /* Invalid BRR is a hard error; never silently fall back to 64. */
    if(!BrrValid(tx) || !BrrValid(rx)) {
        scan_error=9U;scan_state=STATE_ERROR;return;
    }
    scan_tx_brr_active=tx;
    scan_rx_brr_active=rx;
    scan_brr=rx;
    swd_uart_brr=rx;
    rx_frame_cycles=11U*rx;
    first_word_wait_cycles=rx_frame_cycles+7U*CYC_PER_US;
    if(first_word_wait_cycles<22U*CYC_PER_US) first_word_wait_cycles=22U*CYC_PER_US;
    silence_gap_cycles=rx_frame_cycles+7U*CYC_PER_US;
    if(silence_gap_cycles<GAP_CYC) silence_gap_cycles=GAP_CYC;
    tx_wait_cycles=22U*tx;
    if(tx_wait_cycles<48000U) tx_wait_cycles=48000U;
    block_used=0U;block_records=0U;block_seq=0U;
    block_start=start;
    scan_phase=phase;
    scan_next_order=start;
    scan_total=total;
    scan_chunk_seq=0U;scan_ack_seq=0U;
    scan_chunk_count=0U;scan_chunk_bytes=0U;scan_chunk_crc32=0U;
    scan_cmd_req_seq=0U;scan_cmd_ready_seq=0U;
    scan_cmd_stage=0U;scan_cmd_start=0U;scan_cmd_count=0U;
    scan_error=0U;scan_state=STATE_RUN;
    for(order=start;order<total;order++) {
        /* Save partial responses before requesting a fresh input batch. */
        if(phase>=3U && !(scan_cmd_stage==phase &&
            order>=scan_cmd_start && order-scan_cmd_start<scan_cmd_count)) {
            if(block_records) PublishChunk();
            err=NeedHostCommands(phase,order,total);
            if(err) {scan_error=err;scan_state=STATE_ERROR;return;}
        }
        /* Reserve space for the largest possible result BEFORE starting TX. */
        if(block_used+MAX_SCAN_ENTRY>SCAN_BUF_SIZE) PublishChunk();
        err=ScanOne(phase,order);
        if(err) {scan_error=err;scan_state=STATE_ERROR;return;}
        scan_next_order=order+1U;
    }
    if(block_records) PublishChunk();
    scan_state=STATE_DONE;
}
/* Old OpenCode v17 mailbox only when scan is idle / finished. */
static void LegacyOne(uint32_t ticket,uint32_t mode,const uint16_t *words,uint32_t n) {
    uint32_t id=swd_log_head+1U,slot=(id-1U)%LEGACY_CAP,i,t;
    volatile NineRecord *r=&swd_log[slot];
    r->sequence=0U;r->ticket=ticket;r->mode=mode;r->tx_len=n;
    r->good_total=0U;r->bad_total=0U;r->error_or=0U;
    r->status=0U;r->rx_window_us=200U;r->time_ms=HAL_GetTick();
    r->uart_brr=USART1->BRR;r->reserved=0U;
    for(i=0;i<16;i++)r->tx_words[i]=(n<=16U && i<n)?words[i]:0U;
    for(i=0;i<32;i++){r->rx_good[i]=0U;r->rx_good_us[i]=0U;}
    for(i=0;i<16;i++){r->rx_bad[i]=0U;r->rx_bad_us[i]=0U;r->rx_bad_flags[i]=0U;}
    if(mode!=2U || n<1U || n>16U) r->status=1U;
    else for(i=0;i<n;i++)if(words[i]>0x1FFU)r->status=1U;
    if(!r->status) {
        rx_kind=CAP_NONE;
        t=SendWords(words,n);
        if(!t) r->status=2U;
        else {
            r->reserved=(t-legacy_tx_start)/CYC_PER_US;
            legacy_record=r;
            rx_kind=CAP_LEGACY;
            DelayUs(200U);
            rx_kind=CAP_NONE;
            legacy_record=0;
        }
    }
    swd_last_status=r->status;
    __DMB();r->sequence=id;__DMB();swd_log_head=id;
}
void Error_Handler(void) {
    GPIOB->BRR=GPIO_PIN_0;
    scan_error=0xFFFFFFFFU;
    scan_state=STATE_ERROR;
    __disable_irq();while(1){__NOP();}
}
int main(void) {
    uint32_t req,ticket,phase,start,n,i,mode;
    uint16_t words[16];
    HAL_Init();Clock48();Pins();
    CoreDebug->DEMCR|=CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CYCCNT=0U;DWT->CTRL|=DWT_CTRL_CYCCNTENA_Msk;
    UartInit();
    if(swd_fw_magic!=V17_MAGIC || scan_magic!=SCAN_MAGIC ||
       scan_protocol!=26U ||
       swd_record_size!=304U || scan_buf_capacity!=SCAN_BUF_SIZE ||
       scan_cmd_capacity!=CMDS_PER_BATCH) Error_Handler();
    for(;;) {
        req=scan_request_seq;
        if(req && req!=scan_request_done &&
           (scan_state==STATE_IDLE || scan_state==STATE_DONE)) {
            phase=scan_request_phase;
            start=scan_request_start;
            if(phase<1U || phase>STAGE_COUNT ||
               start>StageTotal(phase)) {
                scan_error=2U;scan_state=STATE_ERROR;
            } else {
                scan_request_done=req;
                ScanPass(phase,start);
            }
        }
        if(scan_state==STATE_IDLE || scan_state==STATE_DONE) {
            ticket=swd_mailbox_ticket;
            if(ticket && ticket!=swd_mailbox_done) {
                swd_mailbox_busy=1U;
                mode=swd_mailbox_mode;n=swd_mailbox_len;
                if(n<=LEGACY_WORDS)
                    for(i=0U;i<n;i++)words[i]=(uint16_t)swd_mailbox_words[i];
                legacy_tx_start=DWT->CYCCNT;
                LegacyOne(ticket,mode,words,n);
                swd_mailbox_done=ticket;
                swd_mailbox_busy=0U;
            }
        }
        DelayUs(100U);
    }
}
