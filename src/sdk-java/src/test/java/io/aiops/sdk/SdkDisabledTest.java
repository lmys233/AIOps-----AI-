package io.aiops.sdk;

import static org.assertj.core.api.Assertions.*;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.runner.WebApplicationContextRunner;
import org.springframework.boot.autoconfigure.AutoConfigurations;

class SdkDisabledTest {
    @Test void optOutDoesNotInstallFilterServerRuntimeOrWrappers(){
        new WebApplicationContextRunner().withConfiguration(AutoConfigurations.of(AiopsAutoConfiguration.class))
            .withPropertyValues("aiops.sdk.enabled=false").run(context->{
                assertThat(context).doesNotHaveBean(SdkRuntime.class);assertThat(context).doesNotHaveBean(SdkLifecycle.class);
                assertThat(context).doesNotHaveBean(ObservationPostProcessor.class);
            });
    }
}
